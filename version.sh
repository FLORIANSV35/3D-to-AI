#!/bin/bash
# Numéro de version "V AA.MM.XX" :
#   AA = année (2 chiffres), MM = mois, XX = nombre de commits faits durant ce mois (remis à zéro chaque début de mois),
#   comptés jusqu'au commit REF inclus (défaut : HEAD). Le mois est celui du commit REF.
# Usage : ./version.sh            -> V26.10.02
#         ./version.sh --plain    -> 26.10.02 (sans le "V")
#         ./version.sh <commit>   -> version d'un autre commit
PLAIN=0; REF=HEAD
for a in "$@"; do case "$a" in --plain) PLAIN=1 ;; *) REF="$a" ;; esac; done
cd "$(dirname "$0")"
YM=$(git log -1 --format=%cd --date=format-local:%y.%m "$REF")
N=$(git log "$REF" --format=%cd --date=format-local:%y.%m | grep -c "^$YM$")
V=$(printf "%s.%02d" "$YM" "$N")
if [ "$PLAIN" = 1 ]; then echo "$V"; else echo "V$V"; fi
