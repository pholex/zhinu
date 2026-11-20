#!/bin/sh
# 跑全部用例
exec python3 -m unittest discover -s tests -t . "$@"
