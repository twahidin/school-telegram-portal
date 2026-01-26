#!/bin/bash
# Script to check for multiple Telegram bot processes

echo "Checking for running bot processes..."
echo ""

# Check for bot.py processes
BOT_PROCESSES=$(ps aux | grep -E "[b]ot.py|[p]ython.*bot" | grep -v grep)

if [ -z "$BOT_PROCESSES" ]; then
    echo "✅ No bot processes found running"
    exit 0
else
    echo "⚠️  Found bot processes:"
    echo "$BOT_PROCESSES"
    echo ""
    echo "To stop all bot processes, run:"
    echo "  pkill -f bot.py"
    echo ""
    echo "Or kill specific processes by PID:"
    echo "  kill <PID>"
    exit 1
fi
