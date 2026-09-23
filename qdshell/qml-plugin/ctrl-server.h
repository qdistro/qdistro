// ctrl-server.h — Unix socket control interface for qdshell.
//
// Listens on $XDG_RUNTIME_DIR/qdshell.sock and serves one command per
// connection (newline-terminated). Follows the same pattern as
// qdlocker's ctrl socket (qdlocker/qdlocker/ctrl.py).
//
// SPDX-License-Identifier: GPL-3.0-or-later

#pragma once

#include <QObject>
#include <QLocalServer>
#include <QString>

class QLocalSocket;
class QdwinBinding;

class CtrlServer : public QObject {
    Q_OBJECT

public:
    explicit CtrlServer(QdwinBinding &binding, QObject *parent = nullptr);
    ~CtrlServer() override;

private slots:
    void onNewConnection();
    void onReadyRead();
    void onClientTimeout();

private:
    void handleConnection(QLocalSocket *sock);
    QString handleCommand(const QString &line);

    QdwinBinding &binding_;
    QLocalServer server_;
    QString listenedPath_;       // exact path we successfully listen()ed on
    bool listening_ = false;     // true only after a successful listen()
};
