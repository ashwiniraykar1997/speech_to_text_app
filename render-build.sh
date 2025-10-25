#!/usr/bin/env bash
# Exit on error
set -o errexit

# Upgrade build tools before installing your dependencies
pip install --upgrade pip setuptools wheel

# Install your project requirements
pip install -r requirements.txt
