import sys
sys.path.insert(0, "/home/admin/qdshell")
sys.path.insert(0, "/home/admin/qdshell/protocol")
from protocol.qdwin_shell_v1 import QdwinShellV1
print("events:")
for i, e in enumerate(QdwinShellV1.events):
    print(f"  [{i}] {e.name}")
