#!/bin/bash
# Install the SC-350 cut software on Ubuntu. Run from the sc350-cut folder:  sudo ./deploy/install.sh
set -euo pipefail
cd "$(dirname "$0")/.."

apt-get update
apt-get install -y python3 python3-reportlab python3-pypdf poppler-utils librsvg2-bin ghostscript cups-client

id sc350cut >/dev/null 2>&1 || useradd --system --home /srv/cut --shell /usr/sbin/nologin sc350cut
usermod -aG lp sc350cut || true

install -d /opt/sc350cut
rm -rf /opt/sc350cut/sc350cut
cp -r sc350cut /opt/sc350cut/
[ -f /etc/sc350cut.ini ] || install -m 644 deploy/sc350cut.ini /etc/sc350cut.ini

install -d -o sc350cut -g sc350cut /srv/cut /srv/cut/hotfolder /srv/cut/queue /srv/cut/archive /srv/cut/plt
# let people on the server drop files in the hot folder
chmod 2777 /srv/cut/hotfolder

install -m 644 deploy/sc350cut.service /etc/systemd/system/sc350cut.service
systemctl daemon-reload
systemctl enable sc350cut
systemctl restart sc350cut
systemctl --no-pager status sc350cut | head -5
echo "Open http://$(hostname -I | awk '{print $1}'):8350"
