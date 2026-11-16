// The mark of a shared workspace: a small two-person icon beside the name, in
// the rail and in the header of the workspace. It carries its own name for a
// screen reader, because it stands for no text on screen.

import Icon from "../shell/Icon";
import { PEOPLE_ICON } from "../shell/sections";

export function SharedMark() {
  return (
    <span className="shared-mark" role="img" aria-label="Shared workspace">
      <Icon paths={PEOPLE_ICON} />
    </span>
  );
}
