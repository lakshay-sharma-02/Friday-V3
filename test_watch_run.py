#!/usr/bin/env python3
import sys
sys.path.insert(0, 'src')
import os

# Run watch --run-once
from friday.cli import main
main(['watch', '--run-once'])