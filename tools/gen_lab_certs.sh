#!/usr/bin/env bash
# Create a throw-away lab PKI: CA, broker certificate, xConnect-receiver certificate, EMS client certificate.
# LAB USE ONLY - production certificates come from the customer's PKI (or SICAM GridPass / EST).
#   ./gen_lab_certs.sh <outdir> <broker-host-or-ip> [receiver-ip]
set -euo pipefail
# Git Bash / MSYS2 on Windows: stop "-subj /CN=..." being rewritten as a Windows path.
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
OUT=${1:-./lab-pki}; BROKER=${2:-127.0.0.1}; RECV=${3:-127.0.0.1}
mkdir -p "$OUT"; cd "$OUT"
san() { if [[ $1 =~ ^[0-9.]+$ ]]; then echo "IP:$1,DNS:localhost,IP:127.0.0.1"; else echo "DNS:$1,DNS:localhost,IP:127.0.0.1"; fi; }
# CA and leaf extensions satisfy strict X.509 checks (e.g. Python 3.13 VERIFY_X509_STRICT)
openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout ca.key -out ca.pem -subj "/CN=FRB Lab CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "subjectKeyIdentifier=hash" >/dev/null 2>&1
mk() {  # name cn san
  openssl req -newkey rsa:2048 -nodes -keyout "$1.key" -out "$1.csr" -subj "/CN=$2" >/dev/null 2>&1
  # extension file on disk, not <(...): a native Windows openssl cannot open /dev/fd/N
  printf "subjectAltName=%s\nextendedKeyUsage=serverAuth,clientAuth\nkeyUsage=critical,digitalSignature,keyEncipherment\nbasicConstraints=CA:FALSE\nauthorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n" "$3" > "$1.ext"
  openssl x509 -req -in "$1.csr" -CA ca.pem -CAkey ca.key -CAcreateserial -days 825 -out "$1.pem" \
    -extfile "$1.ext" >/dev/null 2>&1
  rm -f "$1.csr" "$1.ext"
}
mk broker "$BROKER" "$(san "$BROKER")"
mk receiver "$RECV" "$(san "$RECV")"
mk ems "ems-client" "DNS:ems-client"
chmod 600 ./*.key
echo "Lab PKI written to $(pwd): ca.pem broker.{pem,key} receiver.{pem,key} ems.{pem,key}"
