// BrokerCallRunner against real child processes: every request reports
// exactly once, never before start() has returned its id, and a timed-out
// child is reaped without blocking the event loop.

#include "broker-call.h"

#include <QElapsedTimer>
#include <QSignalSpy>
#include <QTest>
#include <QTimer>

class TestBrokerCall : public QObject {
    Q_OBJECT

private:
    static QStringList sh(const char *script) {
        return {QStringLiteral("-c"), QString::fromUtf8(script)};
    }

private slots:
    // ensures: a normal exit reports its code and stdout once.
    void exitReportsCodeAndStdout() {
        BrokerCallRunner r(QStringLiteral("/bin/sh"));
        QSignalSpy spy(&r, &BrokerCallRunner::finished);
        const int id = r.start(sh("echo 's \"allow\"'; exit 3"), 5000);
        QVERIFY(id > 0);
        QVERIFY(spy.wait(5000));
        QCOMPARE(spy.count(), 1);
        QCOMPARE(spy[0][0].toInt(), id);
        QCOMPARE(spy[0][1].toInt(), 3);
        QCOMPARE(spy[0][2].toString(), QStringLiteral("s \"allow\"\n"));
        QCOMPARE(spy[0][3].toBool(), false);
        QTRY_COMPARE(r.liveChildren(), 0);
        QCOMPARE(r.outstanding(), 0);
    }

    // ensures: a start failure that QProcess emits synchronously (no
    // program) is still delivered after start() returns, exactly once.
    void synchronousStartFailureIsQueued() {
        BrokerCallRunner r{QString()};
        QSignalSpy spy(&r, &BrokerCallRunner::finished);
        const int id = r.start({QStringLiteral("x")}, 5000);
        QCOMPARE(spy.count(), 0);   // not emitted from inside start()
        QCOMPARE(r.outstanding(), 1);
        QVERIFY(spy.wait(2000));
        QCOMPARE(spy.count(), 1);
        QCOMPARE(spy[0][0].toInt(), id);
        QCOMPARE(spy[0][1].toInt(), -1);
        QCOMPARE(spy[0][3].toBool(), false);
        QTRY_COMPARE(r.liveChildren(), 0);
        QTest::qWait(100);
        QCOMPARE(spy.count(), 1);
    }

    // ensures: a missing executable (asynchronous start failure) denies once.
    void missingProgramFailsOnce() {
        BrokerCallRunner r(QStringLiteral("/nonexistent/qdshell-no-busctl"));
        QSignalSpy spy(&r, &BrokerCallRunner::finished);
        const int id = r.start({QStringLiteral("x")}, 5000);
        QCOMPARE(spy.count(), 0);
        QVERIFY(spy.wait(5000));
        QCOMPARE(spy[0][0].toInt(), id);
        QCOMPARE(spy[0][1].toInt(), -1);
        QTRY_COMPARE(r.liveChildren(), 0);
        QTest::qWait(100);
        QCOMPARE(spy.count(), 1);
    }

    // ensures: rejected input (empty args) and the live-child cap answer
    // -1 after start() returns; the cap counts children until reaped.
    void rejectedInputAndCap() {
        BrokerCallRunner r(QStringLiteral("/bin/sh"), 1);
        QSignalSpy spy(&r, &BrokerCallRunner::finished);
        const int rejected = r.start({}, 5000);
        QCOMPARE(spy.count(), 0);
        QCOMPARE(r.liveChildren(), 0);
        const int slow = r.start(sh("sleep 0.5"), 10000);
        QCOMPARE(r.liveChildren(), 1);
        const int capped = r.start(sh("exit 0"), 5000);
        QCOMPARE(r.liveChildren(), 1);
        QTRY_COMPARE(spy.count(), 2);
        QCOMPARE(spy[0][0].toInt(), rejected);
        QCOMPARE(spy[0][1].toInt(), -1);
        QCOMPARE(spy[1][0].toInt(), capped);
        QCOMPARE(spy[1][1].toInt(), -1);
        QVERIFY(slow != rejected && slow != capped);
        QTRY_COMPARE(spy.count(), 3);
        QCOMPARE(spy[2][0].toInt(), slow);
        QCOMPARE(spy[2][1].toInt(), 0);
        QTRY_COMPARE(r.liveChildren(), 0);
    }

    // ensures: a timeout reports (-1, timedOut) once, promptly, without
    // waiting for the child; the late exit is not reported again, and the
    // child stays counted until it has been reaped.
    void timeoutThenLateExit() {
        BrokerCallRunner r(QStringLiteral("/bin/sh"));
        QSignalSpy spy(&r, &BrokerCallRunner::finished);
        // The child ignores SIGTERM; only the runner's SIGKILL stops it.
        QElapsedTimer t;
        t.start();
        const int id = r.start(sh("trap '' TERM; echo early; sleep 30"), 200);
        QVERIFY(t.elapsed() < 1000);   // start() itself does not wait
        // The event loop keeps turning while the call is pending.
        int ticks = 0;
        QTimer tick;
        connect(&tick, &QTimer::timeout, [&ticks]() { ++ticks; });
        tick.start(20);
        QVERIFY(spy.wait(5000));
        QVERIFY(t.elapsed() < 2000);
        QVERIFY(ticks > 0);
        QCOMPARE(spy.count(), 1);
        QCOMPARE(spy[0][0].toInt(), id);
        QCOMPARE(spy[0][1].toInt(), -1);
        QCOMPARE(spy[0][2].toString(), QString());
        QCOMPARE(spy[0][3].toBool(), true);
        QTRY_COMPARE(r.liveChildren(), 0);
        QTest::qWait(200);
        QCOMPARE(spy.count(), 1);
        QCOMPARE(r.outstanding(), 0);
    }

    // ensures: concurrent requests each get their own single report.
    void concurrentRequestsReportOnceEach() {
        BrokerCallRunner r(QStringLiteral("/bin/sh"));
        QSignalSpy spy(&r, &BrokerCallRunner::finished);
        QList<int> ids;
        for (int i = 0; i < 8; ++i)
            ids << r.start(sh("sleep 0.1; echo ok"), 5000);
        QTRY_COMPARE_WITH_TIMEOUT(spy.count(), 8, 10000);
        QSet<int> seen;
        for (const auto &args : spy) {
            QVERIFY(ids.contains(args[0].toInt()));
            seen.insert(args[0].toInt());
            QCOMPARE(args[1].toInt(), 0);
        }
        QCOMPARE(seen.size(), 8);
        QTRY_COMPARE(r.liveChildren(), 0);
    }
};

QTEST_GUILESS_MAIN(TestBrokerCall)
#include "tst_broker_call.moc"
