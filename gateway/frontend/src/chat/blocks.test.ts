import { describe, expect, it } from "vitest";
import { ARTIFACT_INFO, SPEECH_INFO, isInfo, splitFence } from "./blocks";

describe("splitFence with the speech info string", () => {
  it("takes the speech block out of the answer", () => {
    const text = "## Payback\n\nAbout **11 years**.\n\n```speech\nAbout eleven years at today's rates.\n```\n";
    expect(splitFence(text, SPEECH_INFO)).toEqual({
      display: "## Payback\n\nAbout **11 years**.",
      content: "About eleven years at today's rates.",
    });
  });

  it("takes the last block, and removes every one from the display", () => {
    const text = "One.\n\n```speech\nfirst\n```\n\nTwo.\n\n```speech\nsecond\n```";
    expect(splitFence(text, SPEECH_INFO)).toEqual({ display: "One.\n\nTwo.", content: "second" });
  });

  it("takes a ~~~ fence and a longer fence", () => {
    expect(splitFence("A.\n~~~speech\nSaid.\n~~~", SPEECH_INFO).content).toBe("Said.");
    expect(splitFence("A.\n````speech\nSaid.\n```\nstill\n````", SPEECH_INFO).content).toBe("Said.\n```\nstill");
  });

  it("takes the rest of the text for a block with no closing fence", () => {
    expect(splitFence("Answer.\n\n```speech\nSaid so far", SPEECH_INFO)).toEqual({ display: "Answer.", content: "Said so far" });
  });

  it("returns the whole text with no block", () => {
    const text = "Plain.\n\n```python\nx = 1\n```\n\n";
    expect(splitFence(text, SPEECH_INFO)).toEqual({ display: text, content: null });
    expect(splitFence("", SPEECH_INFO)).toEqual({ display: "", content: null });
  });

  it("matches the info string with no regard to case or spaces", () => {
    expect(splitFence("A.\n```  Speech  \nSaid.\n```", SPEECH_INFO).content).toBe("Said.");
    expect(splitFence("A.\n```SPEECH\nSaid.\n```", SPEECH_INFO).content).toBe("Said.");
    expect(splitFence("A.\n```speeches\nSaid.\n```", SPEECH_INFO).content).toBeNull();
  });

  it("removes the blank lines around a block", () => {
    expect(splitFence("Top.\n\n\n```speech\nS.\n```\n\n\nBottom.\n\n", SPEECH_INFO).display).toBe("Top.\n\nBottom.");
    expect(splitFence("```speech\nS.\n```\n\nAfter.", SPEECH_INFO).display).toBe("After.");
    expect(splitFence("```speech\nS.\n```", SPEECH_INFO)).toEqual({ display: "", content: "S." });
  });

  it("gives null for an empty block, and still hides it", () => {
    expect(splitFence("A.\n\n```speech\n  \n```", SPEECH_INFO)).toEqual({ display: "A.", content: null });
  });

  it("leaves a speech fence inside another code block alone", () => {
    const text = "Example:\n\n````markdown\n```speech\nx\n```\n````";
    expect(splitFence(text, SPEECH_INFO)).toEqual({ display: text, content: null });
  });

  it("hides a last line that can still become a speech fence while streaming", () => {
    expect(splitFence("Answer.\n\n```spe", SPEECH_INFO, true).display).toBe("Answer.");
    expect(splitFence("Answer.\n\n```", SPEECH_INFO, true).display).toBe("Answer.");
    expect(splitFence("Answer.\n\n```py", SPEECH_INFO, true).display).toBe("Answer.\n\n```py");
    expect(splitFence("Answer.\n\n```spe", SPEECH_INFO).display).toBe("Answer.\n\n```spe");
    // A closing fence on the last line is not an opening one.
    expect(splitFence("```py\nx\n```", SPEECH_INFO, true).display).toBe("```py\nx\n```");
  });
});

describe("splitFence", () => {
  it("takes an artifact block out of the answer", () => {
    const text = "Written.\n\n```artifact\ndocuments/sam/soup.md\n```\n";
    expect(splitFence(text, ARTIFACT_INFO)).toEqual({
      display: "Written.",
      content: "documents/sam/soup.md",
    });
  });

  it("leaves a block of another info string alone", () => {
    const text = "A.\n\n```speech\nSaid.\n```";
    expect(splitFence(text, ARTIFACT_INFO)).toEqual({ display: text, content: null });
  });

  it("hides a last line that can still become an artifact fence while streaming", () => {
    expect(splitFence("Written.\n\n```arti", ARTIFACT_INFO, true).display).toBe("Written.");
    expect(splitFence("Written.\n\n```arti", ARTIFACT_INFO).display).toBe("Written.\n\n```arti");
  });

  it("matches the info string with no regard to case or spaces", () => {
    expect(isInfo("  Artifact ", ARTIFACT_INFO)).toBe(true);
    expect(isInfo("artifacts", ARTIFACT_INFO)).toBe(false);
  });
});
