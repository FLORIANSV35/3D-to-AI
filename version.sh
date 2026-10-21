#!/bin/bash
# Version number "V YY.MM.XX" :
#   YY = year (2 digits), MM = month, XX = number of commits made during that month (reset to zero at the start of each month),
#   counted up to and including the commit REF (default : HEAD). The month is the one of the commit REF.
# Usage : ./version.sh            -> V26.10.02
#         ./version.sh --plain    -> 26.10.02 (without the "V")
#         ./version.sh <commit>   -> version of another commit
PLAIN=0; REF=HEAD
for a in "$@"; do case "$a" in --plain) PLAIN=1 ;; *) REF="$a" ;; esac; done
cd "$(dirname "$0")"
YM=$(git log -1 --format=%cd --date=format-local:%y.%m "$REF")
N=$(git log "$REF" --format=%cd --date=format-local:%y.%m | grep -c "^$YM$")
V=$(printf "%s.%02d" "$YM" "$N")
if [ "$PLAIN" = 1 ]; then echo "$V"; else echo "V$V"; fi
