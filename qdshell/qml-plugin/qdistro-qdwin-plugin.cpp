// Plugin entrypoint — registers QdwinBinding under Qdistro.Qdwin 1.0.
//
// SPDX-License-Identifier: GPL-3.0-or-later

#include <QQmlEngine>
#include <QQmlExtensionPlugin>

#include "qdwin-binding.h"

class QdistroQdwinPlugin : public QQmlExtensionPlugin {
    Q_OBJECT
    Q_PLUGIN_METADATA(IID QQmlExtensionInterface_iid)

public:
    void registerTypes(const char *uri) override {
        Q_ASSERT(uri == QLatin1String("Qdistro.Qdwin"));
        qmlRegisterType<QdwinBinding>(uri, 1, 0, "QdwinBinding");
    }
};

#include "qdistro-qdwin-plugin.moc"
