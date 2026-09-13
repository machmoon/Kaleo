// Voice settings: the on/off switch and the optional ElevenLabs upgrade.
//
// The default needs no configuration at all — the webview's built-in
// speechSynthesis speaks for free — so this panel's whole job is the
// upgrade path: paste a key and the digest switches to ElevenLabs. The
// selection rule is exactly "key present", mirrored from
// `chooseBackendName` in `src/lib/speech/speaker.ts`.
//
// The key field is `type="password"` and its value goes nowhere but
// localStorage and the `xi-api-key` header. No test button on purpose: a
// "try the voice" call would spend the user's ElevenLabs quota to render a
// checkmark.

import { useState } from "react";
import { Button, Header, Input, Label, Switch } from "@/components";
import {
  isVoiceEnabled,
  loadElevenLabsKey,
  loadElevenLabsVoiceId,
  saveElevenLabsKey,
  saveElevenLabsVoiceId,
  saveVoiceEnabled,
  speaker,
  webSpeechAvailable,
} from "@/lib/speech";
import { cn } from "@/lib/utils";

export const VoiceSettings = ({ className }: { className?: string }) => {
  const [enabled, setEnabled] = useState(isVoiceEnabled);
  const [keyDraft, setKeyDraft] = useState(loadElevenLabsKey);
  const [voiceDraft, setVoiceDraft] = useState(loadElevenLabsVoiceId);
  const [savedKey, setSavedKey] = useState(loadElevenLabsKey);

  const toggle = (next: boolean) => {
    setEnabled(next);
    saveVoiceEnabled(next);
    if (!next) speaker.stop();
  };

  const save = () => {
    const key = keyDraft.trim();
    const voice = voiceDraft.trim();
    saveElevenLabsKey(key);
    saveElevenLabsVoiceId(voice);
    setKeyDraft(key);
    setVoiceDraft(voice);
    setSavedKey(key);
  };

  const dirty =
    keyDraft.trim() !== savedKey || voiceDraft.trim() !== loadElevenLabsVoiceId();

  return (
    <div id="voice-settings" className={cn("space-y-3", className)}>
      <Header
        title="Voice"
        description="When a run finishes, Hardy reads the review's headline findings aloud"
        isMainTitle
      />

      <div className="flex items-center gap-2">
        <Switch
          id="voice-enabled"
          data-testid="voice-enabled"
          checked={enabled}
          onCheckedChange={toggle}
        />
        <Label htmlFor="voice-enabled" className="text-sm">
          Speak a digest when a run completes
        </Label>
      </div>

      {!webSpeechAvailable() && !savedKey ? (
        <p className="text-xs text-muted-foreground">
          This webview has no built-in speech synthesis, so the free voice is
          unavailable here. An ElevenLabs key below enables voice anyway.
        </p>
      ) : null}

      <div className="flex flex-col gap-2">
        <Label htmlFor="elevenlabs-key" className="text-sm font-medium">
          ElevenLabs API key <span className="text-muted-foreground">(optional)</span>
        </Label>
        <Input
          id="elevenlabs-key"
          data-testid="elevenlabs-key"
          type="password"
          autoComplete="off"
          value={keyDraft}
          placeholder="leave empty to use the built-in voice"
          onChange={(event) => setKeyDraft(event.target.value)}
          className="max-w-96"
        />
        <Label htmlFor="elevenlabs-voice" className="text-sm font-medium">
          Voice id <span className="text-muted-foreground">(optional)</span>
        </Label>
        <Input
          id="elevenlabs-voice"
          data-testid="elevenlabs-voice"
          value={voiceDraft}
          placeholder="default voice when empty"
          onChange={(event) => setVoiceDraft(event.target.value)}
          className="max-w-96"
        />
        <div className="flex items-center gap-2">
          <Button size="sm" onClick={save} disabled={!dirty} data-testid="voice-save">
            Save
          </Button>
          <span className="text-[11px] text-muted-foreground">
            {savedKey
              ? "Using ElevenLabs — the key is stored on this machine only."
              : "Using the free built-in voice."}
          </span>
        </div>
      </div>
    </div>
  );
};
