#!/bin/bash
# Stop all SOLUNA Surround processes.
pkill -f "soluna-surround/play.py" 2>/dev/null
pkill -f "soluna-surround/source.py" 2>/dev/null
pkill -f "soluna-surround/server.py" 2>/dev/null
echo "⏹  SOLUNA Surround stopped."
