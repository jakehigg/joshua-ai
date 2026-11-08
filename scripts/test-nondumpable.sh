#!/bin/sh
# Run assignments/tests/test_nondumpable.py in the runtime image of the
# assignments container, with plain Docker. It proves on Linux that a session,
# as the same user, cannot read the environment of the non-dumpable manager.
# For a developer on another system; the unit suite runs the same test on
# Linux. The secrets in the test are fake values.
#
#   sh scripts/test-nondumpable.sh
set -eu

IMAGE="${IMAGE:-joshua-ai-assignments:nondumpable-test}"
cd "$(dirname "$0")/.."

docker build -f assignments/Dockerfile --target runtime -t "$IMAGE" .
docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
	--entrypoint python "$IMAGE" assignments/tests/test_nondumpable.py
