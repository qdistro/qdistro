// ctrl-server.cpp — see ctrl-server.h for shape + rationale.
//
// SPDX-License-Identifier: GPL-3.0-or-later

#include "ctrl-server.h"
#include "qdwin-binding.h"

#include <QDebug>
#include <QFile>
#include <QLocalSocket>
#include <QTimer>

#include <cstdlib>
#include <unistd.h>
#include <sys/socket.h>
#include <sys/stat.h>

namespace {

// Per-client timeout (ms) — if a connected client does not deliver a
// complete newline-terminated command within this window we close it.
// Covers both the "never sends data" and "sends partial data then
// stalls" cases without blocking the Qt event loop.
constexpr int kClientTimeoutMs = 2000;

// Maximum bytes we'll buffer per client before rejecting.
constexpr qint64 kMaxCommandLen = 1024;

QString socketPath() {
    const char *xdg = std::getenv("XDG_RUNTIME_DIR");
    if (xdg && xdg[0])
        return QStringLiteral("%1/qdshell.sock").arg(QString::fromUtf8(xdg));
    return QStringLiteral("/run/user/%1/qdshell.sock").arg(getuid());
}

// Probe whether a Unix socket path is actively served by attempting
// a synchronous client connect. Returns true if a server accepted
// the connection (i.e. the socket is live, not stale).
bool isSocketLive(const QString &path) {
    QLocalSocket probe;
    probe.connectToServer(path, QIODevice::ReadOnly);
    bool live = probe.waitForConnected(200);
    probe.disconnectFromServer();
    return live;
}

} // namespace

CtrlServer::CtrlServer(QdwinBinding &binding, QObject *parent)
    : QObject(parent)
    , binding_(binding)
{
    const QString path = socketPath();

    // Only remove a stale socket — one that exists on disk but has no
    // live server behind it. If a live server is already listening we
    // leave its socket alone and fail gracefully below so the
    // incumbent isn't disrupted.
    if (QFile::exists(path) && !isSocketLive(path))
        QFile::remove(path);

    server_.setSocketOptions(QLocalServer::UserAccessOption);

    if (!server_.listen(path)) {
        qWarning().noquote()
            << "ctrl-server: failed to listen on" << path
            << "—" << server_.errorString();
        return;
    }

    // Record that *we* own this path so the destructor only removes
    // a socket it actually created — a second qdshell instance won't
    // accidentally unlink an active server's socket on exit.
    listenedPath_ = path;
    listening_ = true;

    // Tighten permissions to 0600 (belt-and-braces; UserAccessOption
    // already restricts on most platforms).
    ::chmod(path.toUtf8().constData(), 0600);

    connect(&server_, &QLocalServer::newConnection,
            this, &CtrlServer::onNewConnection);

    qInfo().noquote() << "ctrl-server: listening on" << path;
}

CtrlServer::~CtrlServer() {
    server_.close();
    if (listening_) {
        QFile::remove(listenedPath_);
        listening_ = false;
    }
}

void CtrlServer::onNewConnection() {
    while (QLocalSocket *sock = server_.nextPendingConnection()) {
        connect(sock, &QLocalSocket::disconnected,
                sock, &QLocalSocket::deleteLater);

        // Cap the internal read buffer so a misbehaving client cannot
        // cause unbounded memory growth by flooding data without a
        // newline. +1 so a full kMaxCommandLen line plus '\n' fits.
        sock->setReadBufferSize(kMaxCommandLen + 1);

        if (sock->canReadLine()) {
            // A complete line is already buffered (the common case
            // for socat / echo).
            handleConnection(sock);
        } else {
            // Wire readyRead so we get called back as more data
            // arrives — fully async, never blocks the event loop.
            connect(sock, &QLocalSocket::readyRead,
                    this, &CtrlServer::onReadyRead);

            // Arm a per-client timeout so a misbehaving connector
            // that never completes a line gets cleaned up.
            auto *timer = new QTimer(sock);  // parented to sock
            timer->setSingleShot(true);
            connect(timer, &QTimer::timeout,
                    this, &CtrlServer::onClientTimeout);
            timer->start(kClientTimeoutMs);
        }
    }
}

void CtrlServer::onReadyRead() {
    auto *sock = qobject_cast<QLocalSocket *>(sender());
    if (!sock) return;

    // If the client has buffered more than kMaxCommandLen without a
    // newline, reject immediately.
    if (sock->bytesAvailable() > kMaxCommandLen) {
        disconnect(sock, &QLocalSocket::readyRead,
                   this, &CtrlServer::onReadyRead);
        sock->write(QByteArrayLiteral("error: command too long\n"));
        sock->flush();
        sock->disconnectFromServer();
        return;
    }

    // Wait until a full line is available — the client may deliver
    // "status\n" across multiple TCP-style chunks.
    if (!sock->canReadLine())
        return;

    // Disconnect so we handle exactly one command per connection.
    disconnect(sock, &QLocalSocket::readyRead,
               this, &CtrlServer::onReadyRead);

    handleConnection(sock);
}

void CtrlServer::onClientTimeout() {
    auto *timer = qobject_cast<QTimer *>(sender());
    if (!timer) return;

    // The timer is parented to the socket.
    auto *sock = qobject_cast<QLocalSocket *>(timer->parent());
    if (!sock) return;

    qWarning().noquote() << "ctrl-server: client timed out, closing";
    sock->disconnectFromServer();
    sock->deleteLater();
}

void CtrlServer::handleConnection(QLocalSocket *sock) {
    // Protocol: one line per connection, max 1 KiB.
    QByteArray data = sock->readLine(kMaxCommandLen);

    // Reject overlong / truncated lines: a well-formed command must
    // end with '\n'. readLine returns at most kMaxCommandLen bytes;
    // if the last byte is not '\n', the client either sent a line
    // longer than the protocol allows or closed without a newline.
    if (!data.isEmpty() && !data.endsWith('\n')) {
        sock->write(QByteArrayLiteral("error: command too long or unterminated\n"));
        sock->flush();
        sock->disconnectFromServer();
        return;
    }

    QString line = QString::fromUtf8(data).trimmed();

    const QString cmd = line.section(QLatin1Char(' '), 0, 0);
    if (cmd == QLatin1String("capture")) {
        // The shell is an authorized compositor capture client, so its IPC
        // must not become a same-uid confused deputy. The QGA harness reaches
        // this socket as root; ordinary/admin peers remain denied even though
        // the socket's existing 0600 DAC mode permits its owner to connect.
        struct ucred cred {};
        socklen_t credLen = sizeof(cred);
        qintptr fd = sock->socketDescriptor();
        if (fd < 0 ||
            ::getsockopt(static_cast<int>(fd), SOL_SOCKET, SO_PEERCRED,
                         &cred, &credLen) != 0 ||
            credLen != sizeof(cred) || cred.uid != 0) {
            sock->write(QByteArrayLiteral(
                "error: capture requires authenticated root peer\n"));
            sock->flush();
            sock->disconnectFromServer();
            return;
        }
    }

    QString reply = handleCommand(line);

    sock->write((reply + QStringLiteral("\n")).toUtf8());
    sock->flush();
    sock->disconnectFromServer();
}

QString CtrlServer::handleCommand(const QString &line) {
    if (line.isEmpty())
        return QStringLiteral("error: empty command");

    const int sp = line.indexOf(QLatin1Char(' '));
    const QString cmd = (sp >= 0) ? line.left(sp) : line;

    if (cmd == QLatin1String("last-overlay-keys")) {
        return QStringLiteral("count=%1 last-role=%2 last-sym=%3 last-utf8=\"%4\"")
            .arg(binding_.overlayKeyCount())
            .arg(binding_.lastOverlayRole())
            .arg(binding_.lastOverlaySym())
            .arg(binding_.lastOverlayUtf8());
    }

    if (cmd == QLatin1String("status")) {
        return QStringLiteral("ok");
    }

    if (cmd == QLatin1String("capture")) {
        const QStringList args = line.split(QLatin1Char(' '),
                                            Qt::SkipEmptyParts);
        if (args.size() != 3 && args.size() != 4)
            return QStringLiteral(
                "error: usage: capture <output> <absolute-path> [timeout-ms]");
        int timeoutMs = 0;
        if (args.size() == 4) {
            bool ok = false;
            timeoutMs = args.at(3).toInt(&ok);
            // Bounded so a harness typo cannot park the shell in the capture
            // pump for minutes; 0 would silently mean "default", so refuse it.
            if (!ok || timeoutMs < 1000 || timeoutMs > 120000)
                return QStringLiteral(
                    "error: timeout-ms must be an integer in [1000, 120000]");
        }
        QVariantMap result = binding_.captureOutput(args.at(1), args.at(2),
                                                    timeoutMs);
        if (!result.value(QStringLiteral("ok")).toBool())
            return QStringLiteral("error: %1")
                .arg(result.value(QStringLiteral("error")).toString());
        QString reply =
            QStringLiteral("ok output=%1 width=%2 height=%3 path=%4")
                .arg(result.value(QStringLiteral("output")).toString())
                .arg(result.value(QStringLiteral("width")).toInt())
                .arg(result.value(QStringLiteral("height")).toInt())
                .arg(result.value(QStringLiteral("path")).toString());
        // Stale-served captures (compositor retained-frame fallback) are
        // flagged explicitly; the live reply stays byte-identical so older
        // harness parsers keep working.
        if (result.contains(QStringLiteral("live")) &&
            !result.value(QStringLiteral("live")).toBool())
            reply += QStringLiteral(" live=0 age_ms=%1 msc=%2")
                         .arg(result.value(QStringLiteral("staleAgeMs"))
                                  .toULongLong())
                         .arg(result.value(QStringLiteral("staleMsc"))
                                  .toULongLong());
        return reply;
    }

    return QStringLiteral("error: unknown command '%1'").arg(cmd);
}
