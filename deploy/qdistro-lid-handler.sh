#!/bin/bash
# Handle lid close events and trigger locker

case "$1" in
    lock)
        # Trigger the locker via D-Bus
        if command -v qdbus >/dev/null 2>&1; then
            # Try to activate the locker via qdshell panel service
            qdbus org.kde.qdshell-$(pgrep -n qdshell) /MainApplication org.qtproject.Qt.QCoreApplication.quit 2>/dev/null &
            # Alternative: try to call the locker directly
            pgrep qdlocker >/dev/null && pkill -USR1 qdlocker || qdlocker --lock &
        elif command -v busctl >/dev/null 2>&1; then
            # Try using D-Bus via busctl if available
            busctl call com.qdistro.SessionManager1 /com/qdistro/SessionManager1 com.qdistro.SessionManager1 LockSession 2>/dev/null &
        fi
        
        # As a fallback, directly call the locker if it's available
        if [ -x "/usr/bin/qdlocker" ]; then
            # Check if locker is already running
            if pgrep qdlocker >/dev/null; then
                # Send a signal to the locker to lock the screen
                pkill -USR1 qdlocker 2>/dev/null || \
                gdbus call --session --dest org.kde.qdshell --object-path /LockScreen --method org.kde.qdshell.LockScreen.SetActive true 2>/dev/null &
            else
                # Start the locker if it's not running
                /usr/bin/qdlocker &
            fi
        fi
        ;;
    *)
        logger "qdistro-lid-handler: Unknown action '$1'"
        exit 1
        ;;
esac

exit 0