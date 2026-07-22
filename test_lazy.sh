#!/bin/bash
set -e

export WORKSPACE=$(mktemp -d)
cd $WORKSPACE

export HOME=$WORKSPACE
export FRIDAY_DB=$WORKSPACE/friday.db

echo "Executing open-ended judgment goal..."
time PYTHONPATH=/home/lakshay/Projects/Friday\ V3/src python3 -m friday.cli execute "output the current working directory and list files"
