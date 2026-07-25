#!/usr/bin/env sh
set -eu

REPOSITORY="https://github.com/Google-Health-API/google-health-cli.git"
COMMIT="9cf02743d9ca051500b7c1c181eb88a9ae8988a5"
INSTALL_DIR=${XDG_BIN_HOME:-"$HOME/.local/bin"}
DRY_RUN=0
FORCE=0
WORK_DIR=""
INSTALL_TEMP=""

usage() {
  printf '%s\n' \
    "Usage: scripts/install_ghealth.sh [--install-dir PATH] [--force] [--dry-run]" \
    "" \
    "Build and install the Google-Health-API organization's ghealth CLI at a" \
    "verified commit. This project does not represent it as an official Google product." \
    "ghealth authorization is a separate, explicit step; this installer never reads" \
    "or writes Google credentials."
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --install-dir)
      [ "$#" -ge 2 ] || { echo "--install-dir needs a path" >&2; exit 2; }
      INSTALL_DIR=$2
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

cleanup() {
  if [ -n "$INSTALL_TEMP" ] && [ -e "$INSTALL_TEMP" ]; then
    rm -f -- "$INSTALL_TEMP"
  fi
  if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
    rm -rf -- "$WORK_DIR"
  fi
}
trap cleanup EXIT HUP INT TERM

printf '%s\n' \
  "Source: $REPOSITORY" \
  "Pinned commit: $COMMIT" \
  "Destination: $INSTALL_DIR/ghealth" \
  "This is the Google-Health-API organization's open-source CLI; no claim of official Google product status is made."

if [ "$DRY_RUN" -eq 1 ]; then
  printf '%s\n' \
    "Dry run only: would fetch the exact commit, verify it, build with Go, and atomically install the binary." \
    "No credentials would be configured."
  exit 0
fi

command -v git >/dev/null 2>&1 || { echo "git is required" >&2; exit 1; }
command -v go >/dev/null 2>&1 || { echo "Go 1.23 or newer is required" >&2; exit 1; }

GO_VERSION=$(go env GOVERSION 2>/dev/null || true)
case "$GO_VERSION" in
  go1.*)
    GO_MINOR=$(printf '%s' "$GO_VERSION" | sed -E 's/^go1\.([0-9]+).*/\1/')
    case "$GO_MINOR" in
      ''|*[!0-9]*) echo "Could not verify Go version: $GO_VERSION" >&2; exit 1 ;;
    esac
    [ "$GO_MINOR" -ge 23 ] || { echo "Go 1.23 or newer is required; found $GO_VERSION" >&2; exit 1; }
    ;;
  *)
    echo "Could not verify Go version: $GO_VERSION" >&2
    exit 1
    ;;
esac

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/open-health-agent-ghealth.XXXXXX")
SOURCE_DIR="$WORK_DIR/source"
BUILD_OUTPUT="$WORK_DIR/ghealth"

git init -q "$SOURCE_DIR"
git -C "$SOURCE_DIR" remote add origin "$REPOSITORY"
git -C "$SOURCE_DIR" fetch --quiet --depth 1 origin "$COMMIT"
git -C "$SOURCE_DIR" checkout --quiet --detach FETCH_HEAD
ACTUAL_COMMIT=$(git -C "$SOURCE_DIR" rev-parse HEAD)
if [ "$ACTUAL_COMMIT" != "$COMMIT" ]; then
  echo "Commit verification failed: expected $COMMIT, got $ACTUAL_COMMIT" >&2
  exit 1
fi

(
  cd "$SOURCE_DIR"
  go build -trimpath -ldflags=-buildid= -o "$BUILD_OUTPUT" .
)

mkdir -p -- "$INSTALL_DIR"
DESTINATION="$INSTALL_DIR/ghealth"
if [ -e "$DESTINATION" ] || [ -L "$DESTINATION" ]; then
  if cmp -s "$BUILD_OUTPUT" "$DESTINATION"; then
    echo "Already installed and unchanged: $DESTINATION"
    exit 0
  fi
  if [ "$FORCE" -ne 1 ]; then
    echo "A different file already exists at $DESTINATION; use --force to replace it." >&2
    exit 2
  fi
fi

INSTALL_TEMP=$(mktemp "$INSTALL_DIR/.ghealth.install.XXXXXX")
cp -- "$BUILD_OUTPUT" "$INSTALL_TEMP"
chmod 0755 "$INSTALL_TEMP"
mv -f -- "$INSTALL_TEMP" "$DESTINATION"
INSTALL_TEMP=""
echo "Installed: $DESTINATION"
echo "Authorization was not configured. Review the data scopes before running ghealth auth."
