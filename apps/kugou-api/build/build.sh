#!/bin/sh
# Build the KuGou API image on the server from a source tarball downloaded
# where GitHub is reachable:
#   curl -L -o kugou.tar.gz https://codeload.github.com/MakcRe/KuGouMusicApi/tar.gz/<commit>
#   sh build.sh kugou.tar.gz <commit>
set -e
src_tar=$1; commit=$2
dir=$(mktemp -d)
mkdir "$dir/src"
tar xzf "$src_tar" -C "$dir/src" --strip-components=1
cp "$(dirname "$0")/Dockerfile" "$dir/Dockerfile"
sudo docker build -t "xiaomi-speaker/kugou-api:$commit" "$dir"
rm -rf "$dir"
