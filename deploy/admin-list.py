#!/usr/bin/env python3
"""Print pending requests as id|uid|action|details (one per line)."""
import dbus

BUS = "com.qdistro.AdminBroker1"
PATH = "/com/qdistro/AdminBroker1"


def main():
    bus = dbus.SystemBus()
    obj = bus.get_object(BUS, PATH)
    raw = obj.GetPending(dbus_interface=BUS)
    for r in raw:
        details = ",".join(f"{k}={v}" for k, v in dict(r["details"]).items())
        print(f"{int(r['id'])}|{int(r['uid'])}|{r['action']}|{details}")


if __name__ == "__main__":
    main()
