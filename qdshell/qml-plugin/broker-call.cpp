// broker-call — see broker-call.h for the contract.

#include "broker-call.h"

#include <QMetaObject>
#include <QProcess>
#include <QTimer>

#include <limits>
#include <memory>
#include <utility>

BrokerCallRunner::BrokerCallRunner(QString program, int maxLiveChildren,
                                   QObject *parent)
    : QObject(parent),
      program_(std::move(program)),
      maxLiveChildren_(maxLiveChildren) {}

int BrokerCallRunner::allocateId() {
    do {
        if (nextId_ == std::numeric_limits<int>::max())
            nextId_ = 0;
        ++nextId_;
    } while (outstanding_.contains(nextId_));
    outstanding_.insert(nextId_);
    return nextId_;
}

// Always queued, so no result is delivered before start() has returned
// the id, whichever QProcess signal (some emitted synchronously from
// QProcess::start) produced it.
void BrokerCallRunner::publish(int requestId, int exitCode,
                               const QString &out, bool timedOut) {
    QMetaObject::invokeMethod(
        this,
        [this, requestId, exitCode, out, timedOut]() {
            outstanding_.remove(requestId);
            emit finished(requestId, exitCode, out, timedOut);
        },
        Qt::QueuedConnection);
}

int BrokerCallRunner::start(const QStringList &args, int timeoutMs) {
    const int id = allocateId();
    if (args.isEmpty() || liveChildren_ >= maxLiveChildren_) {
        publish(id, -1, {}, false);
        return id;
    }

    auto *proc = new QProcess(this);
    auto *timer = new QTimer(proc);
    timer->setSingleShot(true);
    struct State {
        bool decided = false;
        bool reaped = false;
    };
    auto state = std::make_shared<State>();
    ++liveChildren_;

    // The verdict: first of exit / start failure / timeout wins.
    auto decide = [this, timer, id, state](int exitCode, const QString &out,
                                           bool timedOut) {
        if (state->decided)
            return;
        state->decided = true;
        timer->stop();
        publish(id, exitCode, out, timedOut);
    };
    // Releases the child once it is no longer running; only then may it
    // be deleted without ~QProcess waiting on the GUI thread.
    auto reap = [this, proc, state]() {
        if (state->reaped)
            return;
        state->reaped = true;
        --liveChildren_;
        proc->deleteLater();
    };

    connect(proc, &QProcess::finished, this,
            [decide, reap, proc](int exitCode, QProcess::ExitStatus status) {
                decide(status == QProcess::NormalExit ? exitCode : -1,
                       QString::fromUtf8(proc->readAllStandardOutput()), false);
                reap();
            });
    connect(proc, &QProcess::errorOccurred, this,
            [decide, reap](QProcess::ProcessError error) {
                // FailedToStart is the only error not followed by
                // finished(); a crash reports through finished() above.
                if (error == QProcess::FailedToStart) {
                    decide(-1, {}, false);
                    reap();
                }
            });
    connect(timer, &QTimer::timeout, this, [decide, proc]() {
        decide(-1, {}, true);
        proc->kill();   // finished() follows and reaps it
    });

    proc->setProgram(program_);
    proc->setArguments(args);
    timer->start(timeoutMs);
    proc->start();
    return id;
}
