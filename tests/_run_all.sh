#!/bin/bash
for f in tests/test_*.py; do
  python3 "$f" > /tmp/test_out.log 2>&1
  status=$?
  if [ $status -eq 0 ] && ! grep -q FAIL /tmp/test_out.log; then
    echo "PASS $f"
  else
    echo "FAIL $f"
    cat /tmp/test_out.log
  fi
done
