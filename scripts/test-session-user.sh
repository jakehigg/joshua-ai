#!/bin/sh
# Run assignments/tests/test_session_user.py in the assignments image, as in
# production: a launcher container as uid 1001, and the test (the manager) as
# uid 1000. The test container joins the process namespace of the launcher, so
# a session can see the manager process and the test proves that the kernel
# refuses to show its environment (EACCES). Both containers have no network,
# no capability, and no-new-privileges.
#
# The three secrets are fake values. The script needs Docker.
#
#   sh scripts/test-session-user.sh
set -eu

IMAGE="${IMAGE:-joshua-ai-assignments:session-test}"
cd "$(dirname "$0")/.."

docker build -f assignments/Dockerfile --target session-test -t "$IMAGE" .

work=$(docker volume create)
run=$(docker volume create)
launcher=""
cleanup() {
	if [ -n "$launcher" ]; then docker rm -f "$launcher" >/dev/null 2>&1 || true; fi
	docker volume rm -f "$work" "$run" >/dev/null 2>&1 || true
}
trap cleanup EXIT

harden="--network none --cap-drop ALL --security-opt no-new-privileges"

# Two launchers: the real Claude CLI, and `sh` for the shell test.
# shellcheck disable=SC2086
launcher=$(docker run -d $harden --user 1001:1001 \
	-v "$work:/work" -v "$run:/run/joshua-session" \
	--entrypoint sh "$IMAGE" -c '
		python -m joshua_assignments.launcher --socket /run/joshua-session/shell.sock --cli sh &
		exec python -m joshua_assignments.launcher')

for _ in $(seq 1 50); do
	if docker exec "$launcher" test -S /run/joshua-session/launcher.sock \
		&& docker exec "$launcher" test -S /run/joshua-session/shell.sock; then
		break
	fi
	sleep 0.2
done

# shellcheck disable=SC2086
docker run --rm $harden --pid "container:$launcher" \
	-v "$work:/work" -v "$run:/run/joshua-session" \
	-e DATABASE_URL="postgresql://joshua:fake-database-password@postgres.invalid:5432/joshua" \
	-e ASSIGNMENTS_TOKENS="alex=fake-caller-token-for-alex" \
	-e ADDON_TOKEN="fake-addon-token" \
	-e ASSIGNMENTS_SESSION_SOCKET=/run/joshua-session/launcher.sock \
	-e ASSIGNMENTS_TEST_SHELL_SOCKET=/run/joshua-session/shell.sock \
	"$IMAGE" -p no:cacheprovider -v -rs assignments/tests/test_session_user.py
