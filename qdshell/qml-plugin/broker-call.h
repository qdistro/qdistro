// broker-call — non-blocking busctl calls for the clipboard gates.
//
// QdwinBinding::startCheckClipboardTransfer / startCheckClipboardReceive
// run their broker call through one BrokerCallRunner so the shell's GUI
// thread never waits on the broker. Kept apart from the binding (which
// needs a live compositor) so tests/native/tst_broker_call.cpp can drive
// the real QProcess edges: synchronous start failure, timeout, late exit.
//
// Contract:
//   - start() returns a request id (> 0) and never emits from inside
//     itself; the caller always holds the id before its result arrives.
//   - Every id gets exactly one finished(), delivered from the event loop,
//     for every outcome: exit, crash, start failure, timeout, rejected
//     input (empty args) and the live-child cap. Every edge reports
//     exitCode -1, which the gates deny.
//   - A timeout publishes its result first, then kills the child; the
//     child is deleted only after it has exited, so a slow kill never
//     blocks the GUI thread in ~QProcess. The live-child cap counts
//     children until they are reaped, not just undecided requests.
//   - Ids still awaiting their result are never reissued on wraparound.

#pragma once

#include <QObject>
#include <QSet>
#include <QString>
#include <QStringList>

class BrokerCallRunner : public QObject {
    Q_OBJECT

public:
    explicit BrokerCallRunner(QString program = QStringLiteral("busctl"),
                              int maxLiveChildren = 16,
                              QObject *parent = nullptr);

    int start(const QStringList &args, int timeoutMs);

    // Children started and not yet reaped (counted against the cap).
    int liveChildren() const { return liveChildren_; }
    // Ids whose finished() has not been delivered yet.
    int outstanding() const { return int(outstanding_.size()); }

signals:
    void finished(int requestId, int exitCode, const QString &stdoutText,
                  bool timedOut);

private:
    int allocateId();
    void publish(int requestId, int exitCode, const QString &out,
                 bool timedOut);

    QString program_;
    int maxLiveChildren_;
    int nextId_ = 0;
    int liveChildren_ = 0;
    QSet<int> outstanding_;
};
