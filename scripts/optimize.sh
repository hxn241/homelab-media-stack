#!/bin/bash
# Server tuning for the Docker services.
# Requires sudo.

echo "=== Media server optimization ==="

# 1. Set the CPU governor to schedutil (balance between performance and power saving)
echo "[1/5] Setting CPU governor..."
if [ -f /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor ]; then
    for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        echo "schedutil" | sudo tee "$cpu" > /dev/null 2>&1 || \
        echo "performance" | sudo tee "$cpu" > /dev/null 2>&1
    done
    echo "     CPU governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"
else
    echo "     Cannot change the CPU governor"
fi

# 2. Tune TCP/IP parameters for better responsiveness
echo "[2/5] Tuning network parameters..."
sudo sysctl -w net.ipv4.tcp_keepalive_time=600 > /dev/null 2>&1
sudo sysctl -w net.ipv4.tcp_keepalive_intvl=60 > /dev/null 2>&1
sudo sysctl -w net.ipv4.tcp_keepalive_probes=5 > /dev/null 2>&1
sudo sysctl -w net.core.rmem_max=16777216 > /dev/null 2>&1
sudo sysctl -w net.core.wmem_max=16777216 > /dev/null 2>&1
sudo sysctl -w net.ipv4.tcp_rmem='4096 87380 16777216' > /dev/null 2>&1
sudo sysctl -w net.ipv4.tcp_wmem='4096 65536 16777216' > /dev/null 2>&1
echo "     TCP keepalive reduced to 600s"

# 3. Disable power management on the network interface
echo "[3/5] Disabling network power management..."
for iface in /sys/class/net/enp*/device/power/control /sys/class/net/eth*/device/power/control; do
    if [ -f "$iface" ]; then
        echo "on" | sudo tee "$iface" > /dev/null 2>&1
        echo "     $(dirname $(dirname $iface) | xargs basename): power management disabled"
    fi
done

# 4. Disable disk standby (useful for USB drives)
echo "[4/5] Configuring disks..."
# SATA/SSD disks
for disk in /dev/sd?; do
    if [ -b "$disk" ]; then
        sudo hdparm -B 254 -S 0 "$disk" > /dev/null 2>&1
    fi
done
echo "     Disks configured to never enter standby"

# 5. Show current status
echo "[5/5] Current system status:"
echo "     - CPU governor: $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null || echo 'N/A')"
echo "     - TCP keepalive: $(cat /proc/sys/net/ipv4/tcp_keepalive_time)s"
for state in /sys/class/net/enp*/operstate /sys/class/net/eth*/operstate; do
    [ -f "$state" ] && echo "     - Network $(basename "$(dirname "$state")"): $(cat "$state")"
done

echo ""
echo "=== Optimization complete ==="
echo "NOTE: to make these changes persistent, add to /etc/sysctl.conf:"
echo "      net.ipv4.tcp_keepalive_time=600"
echo "      net.ipv4.tcp_keepalive_intvl=60"
echo "      net.ipv4.tcp_keepalive_probes=5"
