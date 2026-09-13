import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Loader2, SendIcon } from "lucide-react";
import { Button, Input } from "@/components";
import { CliError, listCliTools, runCli } from "@/lib/cli";
import { deliverAuth, deliverConfig, deliverRun, SilkscreenError } from "@/lib/silkscreen/client";
import {
  agendaMinutes,
  badAddresses,
  describeDelivery,
  hintFor,
  needsGoogleSignIn,
  parseAddresses,
  reviewState,
  scheduleNote,
  specReviewOffer,
  whenInPast,
  whenToRfc3339,
  type Destination,
} from "@/lib/silkscreen/deliver";
import type {
  DeliverConfig,
  DeliverRequest,
  SpecReviewBlock,
  StepResponse,
} from "@/lib/silkscreen/types";
import { logEvent } from "@/lib/silkscreen/log";
import { cn } from "@/lib/utils";

export interface DeliverPanelProps {
  baseUrl: string;
  token: string;
  /** The engine's step session id; the board it delivers is that session's routed board. */
  session: string;
  history: readonly StepResponse[];
}

/**
 * Sending the routed board where the team looks: a Chat space, an inbox
 * with the .kicad_pcb attached, a review on the calendar.
 *
 * Mounted under the step panel once copper exists. It asks the engine once
 * what is configured and shows the engine's own hint on anything that is
 * off — the fix lives in the engine's environment, not here. One request in
 * flight at a time: every button is a real send, and a double click would be
 * two emails.
 */
export const DeliverPanel = ({ baseUrl, token, session, history }: DeliverPanelProps) => {
  const [config, setConfig] = useState<DeliverConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [cliGoogleapps, setCliGoogleapps] = useState(false);
  const [emails, setEmails] = useState("");
  const [attendees, setAttendees] = useState("");
  // When to hold the spec review, as the `datetime-local` control holds it.
  // Empty asks the engine for its own slot (`when: null`).
  const [when, setWhen] = useState("");
  const [busy, setBusy] = useState<Destination | "auth" | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  const [lines, setLines] = useState<Partial<Record<Destination, string>>>({});
  const [failures, setFailures] = useState<Partial<Record<Destination, string>>>({});
  const inFlightRef = useRef(false);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    const controller = new AbortController();
    deliverConfig(baseUrl, token, controller.signal)
      .then((found) => {
        if (mountedRef.current) setConfig(found);
      })
      .catch((error: unknown) => {
        if (!mountedRef.current || controller.signal.aborted) return;
        setConfigError((error as Error)?.message ?? "Could not ask the engine what is configured.");
      });
    listCliTools()
      .then((tools) => {
        if (!mountedRef.current) return;
        setCliGoogleapps(tools.some((t) => t.id === "googleapps" && t.available));
      })
      .catch(() => {
        if (mountedRef.current) setCliGoogleapps(false);
      });
    return () => {
      mountedRef.current = false;
      controller.abort();
    };
    // Once per mount: the config is the engine's environment, and a session
    // does not change it. Sign-in refreshes config in place.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const signIn = useCallback(() => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setBusy("auth");
    setAuthError(null);
    void (async () => {
      try {
        // Prefer the checkout CLI: it loads .env and opens the browser from
        // Ada's GUI session. The HTTP flow is for when the CLI is unavailable.
        if (cliGoogleapps) {
          await runCli("googleapps", ["auth"], 320);
          const next = await deliverConfig(baseUrl, token);
          if (!mountedRef.current) return;
          setConfig(next);
        } else {
          const next = await deliverAuth(baseUrl, token);
          if (!mountedRef.current) return;
          setConfig(next);
        }
        logEvent("deliver", "Signed in with Google.");
      } catch (caught) {
        if (!mountedRef.current) return;
        const message =
          caught instanceof CliError || caught instanceof SilkscreenError
            ? caught.message
            : (caught as Error)?.message ?? "sign-in failed";
        setAuthError(message);
      } finally {
        inFlightRef.current = false;
        if (mountedRef.current) setBusy(null);
      }
    })();
  }, [baseUrl, token, cliGoogleapps]);

  const send = useCallback(
    (destination: Destination, payload: DeliverRequest) => {
      if (inFlightRef.current) return;
      inFlightRef.current = true;
      setBusy(destination);
      setFailures((previous) => ({ ...previous, [destination]: undefined }));
      void (async () => {
        try {
          const response = await deliverRun(baseUrl, session, payload, undefined, token);
          if (!mountedRef.current) return;
          const said = describeDelivery(response);
          setLines((previous) => ({ ...previous, ...said }));
          for (const text of Object.values(said)) logEvent("deliver", text);
        } catch (caught) {
          if (!mountedRef.current) return;
          const message =
            caught instanceof SilkscreenError
              ? caught.message
              : (caught as Error)?.message ?? "delivery failed";
          setFailures((previous) => ({ ...previous, [destination]: message }));
        } finally {
          inFlightRef.current = false;
          if (mountedRef.current) setBusy(null);
        }
      })();
    },
    [baseUrl, session, token]
  );

  const review = reviewState(history);
  const emailList = parseAddresses(emails);
  const attendeeList = parseAddresses(attendees);
  const badEmails = badAddresses(emailList);
  const badAttendees = badAddresses(attendeeList);
  const waiting = config === null && configError === null;
  const showSignIn = needsGoogleSignIn(config, cliGoogleapps);

  const chatHint = hintFor(config, "chat");
  const emailHint = hintFor(config, "email");
  const calendarHint = hintFor(config, "calendar");
  // A spec review is a calendar event with a Meet link, so it is off for
  // exactly the reasons the calendar is off, in the same words.
  const specReviewHint = hintFor(config, "spec_review");
  const offer = specReviewOffer(history);
  const whenPast = whenInPast(when);
  const whenNote = whenPast
    ? "That time has already passed; the engine will refuse it."
    : when.trim()
      ? "Books at this time, in this machine's time zone."
      : "Leave the time empty and the engine picks its own slot.";

  const scheduleLine = scheduleNote(history);

  return (
    <div
      className="flex flex-col gap-2 border-t border-input/40 pt-2"
      data-testid="deliver-panel"
    >
      <span className="text-[11px] font-medium">Send it on</span>
      {configError ? (
        <p className="text-[10px] text-destructive" data-testid="deliver-config-error">
          {configError}
        </p>
      ) : null}

      {showSignIn ? (
        <div className="flex flex-col gap-1" data-testid="deliver-signin-row">
          <div className="flex items-center gap-1.5">
            <span className="w-32 shrink-0 text-[11px]">Google account</span>
            <Button
              size="sm"
              variant="outline"
              disabled={waiting || busy !== null}
              title="Opens Google's consent page in your browser for Gmail and Calendar."
              onClick={signIn}
              data-testid="deliver-signin"
            >
              {busy === "auth" ? <Loader2 className="size-3.5 animate-spin" /> : null}
              Sign in with Google
            </Button>
          </div>
          <p className="text-[10px] text-muted-foreground">
            {cliGoogleapps
              ? "Runs `python -m googleapps auth` from this checkout (loads .env). A browser tab asks for Gmail and Calendar."
              : "A browser tab will ask for Gmail and Calendar access. Chat uses a webhook and does not need this."}
          </p>
          {authError ? (
            <p className="text-[10px] text-destructive" data-testid="deliver-signin-error">
              {authError}
            </p>
          ) : null}
        </div>
      ) : null}

      <Row
        destination="chat"
        title="Post to Google Chat"
        hint={chatHint}
        line={lines.chat}
        failure={failures.chat}
      >
        <Button
          size="sm"
          variant="outline"
          disabled={waiting || busy !== null || chatHint !== null}
          title={chatHint ?? "Posts the run card: board, routing, review."}
          onClick={() => send("chat", { chat: true })}
          data-testid="deliver-chat"
        >
          {busy === "chat" ? <Loader2 className="size-3.5 animate-spin" /> : <SendIcon className="size-3.5" />}
          Post
        </Button>
      </Row>

      <Row
        destination="email"
        title="Email the board"
        hint={emailHint}
        line={lines.email}
        failure={failures.email}
        fieldNote={badEmails.length ? `Not an address: ${badEmails.join(", ")}` : null}
      >
        <Input
          className="h-7 flex-1 text-[11px]"
          placeholder="lead@example.com, fab@example.com"
          value={emails}
          disabled={waiting || emailHint !== null}
          onChange={(event) => setEmails(event.target.value)}
          data-testid="deliver-email-to"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={
            waiting ||
            busy !== null ||
            emailHint !== null ||
            emailList.length === 0 ||
            badEmails.length > 0
          }
          title={emailHint ?? "Sends the summary with the .kicad_pcb attached."}
          onClick={() => send("email", { email: emailList })}
          data-testid="deliver-email"
        >
          {busy === "email" ? <Loader2 className="size-3.5 animate-spin" /> : <SendIcon className="size-3.5" />}
          Send
        </Button>
      </Row>

      <Row
        destination="calendar"
        title="Schedule the review"
        hint={calendarHint}
        line={lines.calendar}
        failure={failures.calendar}
        fieldNote={badAttendees.length ? `Not an address: ${badAttendees.join(", ")}` : scheduleLine}
      >
        <Input
          className="h-7 flex-1 text-[11px]"
          placeholder="attendees, comma-separated"
          value={attendees}
          disabled={waiting || calendarHint !== null}
          onChange={(event) => setAttendees(event.target.value)}
          data-testid="deliver-attendees"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={
            waiting ||
            busy !== null ||
            calendarHint !== null ||
            // A failed review found no blockers for the wrong reason; the
            // engine would book nothing and say "no blockers", which is the
            // clean-board reading this refuses.
            review.failed ||
            attendeeList.length === 0 ||
            badAttendees.length > 0
          }
          title={calendarHint ?? review.failure ?? "Books a review only if the review found blockers."}
          onClick={() => send("calendar", { schedule: true, attendees: attendeeList })}
          data-testid="deliver-calendar"
        >
          {busy === "calendar" ? <Loader2 className="size-3.5 animate-spin" /> : <SendIcon className="size-3.5" />}
          Book
        </Button>
      </Row>

      <Row
        destination="spec_review"
        title="Book a spec review"
        hint={specReviewHint}
        line={lines.spec_review}
        failure={failures.spec_review}
        fieldNote={
          badAttendees.length
            ? `Not an address: ${badAttendees.join(", ")}`
            : offer.note ||
              (attendeeList.length === 0 ? `Uses the attendees above. ${whenNote}` : whenNote)
        }
      >
        <Input
          type="datetime-local"
          className="h-7 flex-1 text-[11px]"
          value={when}
          disabled={waiting || specReviewHint !== null}
          onChange={(event) => setWhen(event.target.value)}
          aria-label="When to hold the spec review"
          title="Optional. Empty lets the engine pick the slot."
          data-testid="deliver-spec-review-when"
        />
        <Button
          size="sm"
          variant="outline"
          disabled={
            waiting ||
            busy !== null ||
            specReviewHint !== null ||
            // A failed review: nothing is known about the board, so there is
            // no agenda to put on anyone's calendar (`specReviewOffer`).
            offer.refused ||
            attendeeList.length === 0 ||
            badAttendees.length > 0
          }
          title={
            specReviewHint ??
            (offer.refused ? offer.note : null) ??
            "Puts the agenda below on the calendar with a Meet link, and invites the attendees."
          }
          // `when: null` asks the engine for its own slot. Sent explicitly:
          // an omitted field and "you choose" are different requests. A typed
          // time goes as RFC 3339 with this machine's offset; the engine
          // refuses a past or malformed one with a 400 that lands in the row.
          onClick={() =>
            send("spec_review", {
              spec_review: true,
              attendees: attendeeList,
              when: whenToRfc3339(when),
            })
          }
          data-testid="deliver-spec-review"
        >
          {busy === "spec_review" ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <SendIcon className="size-3.5" />
          )}
          Book the review
        </Button>
      </Row>

      {offer.review ? <Agenda review={offer.review} /> : null}
    </div>
  );
};

/**
 * The agenda, shown before anything is sent.
 *
 * Every number and sentence here is the engine's. Nothing is summed, ranked or
 * reworded except the total, and that only when the engine sent none — the
 * engineer is approving a real meeting, so what is on screen has to be what
 * goes in the invite.
 */
const Agenda = ({ review }: { review: SpecReviewBlock }) => (
  <div
    className="flex flex-col gap-1 rounded-md border border-input/40 p-2"
    data-testid="deliver-agenda"
  >
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-[11px] font-medium">{review.title}</span>
      <span className="text-[10px] text-muted-foreground" data-testid="deliver-agenda-total">
        {agendaMinutes(review)} min
      </span>
    </div>
    {review.summary ? (
      <p className="text-[10px] text-muted-foreground">{review.summary}</p>
    ) : null}
    {review.items.map((item) => (
      <div
        key={item.topic}
        className="flex flex-col"
        data-testid="deliver-agenda-item"
        data-topic={item.topic}
        data-blocking={String(item.blocking)}
      >
        <span className="text-[10px]">
          {item.topic} — {item.minutes} min{item.blocking ? " · blocking" : ""}
        </span>
        <span className="text-[10px] text-muted-foreground">{item.why}</span>
      </div>
    ))}
    {review.decisions_needed?.length ? (
      <p className="text-[10px] text-muted-foreground" data-testid="deliver-agenda-decisions">
        Decisions needed: {review.decisions_needed.join("; ")}
      </p>
    ) : null}
  </div>
);

interface RowProps {
  destination: Destination;
  title: string;
  /** Why the destination is off, in the engine's words; null when ready. */
  hint: string | null;
  /** What happened last time, from the engine's report. */
  line?: string;
  /** A refused request (400/404/409/offline), verbatim. */
  failure?: string;
  fieldNote?: string | null;
  children: ReactNode;
}

const Row = ({ destination, title, hint, line, failure, fieldNote, children }: RowProps) => (
  <div className="flex flex-col gap-1" data-testid="deliver-row" data-destination={destination}>
    <div className="flex items-center gap-1.5">
      <span className={cn("w-32 shrink-0 text-[11px]", hint && "text-muted-foreground/60")}>{title}</span>
      {children}
    </div>
    {hint ? (
      <p className="text-[10px] text-muted-foreground" data-testid="deliver-hint">
        {hint}
      </p>
    ) : fieldNote ? (
      <p className="text-[10px] text-muted-foreground">{fieldNote}</p>
    ) : null}
    {line ? (
      <p className="break-all text-[10px]" data-testid="deliver-result">
        {line}
      </p>
    ) : null}
    {failure ? (
      <p className="text-[10px] text-destructive" data-testid="deliver-failure">
        {failure}
      </p>
    ) : null}
  </div>
);
