/**
 * DecisionTimeoutSection — the global decision-wait timeout (ADR #103 1b).
 *
 * When a task fails it pauses and waits for a human decision (Skip / Mark Success
 * / Fail / Restart) before giving up. This is ONE value for the whole deployment.
 *
 * Tiering: the value is visible to everyone (the GET is free), but only the Team
 * tier can change it. We detect "Team build" by the presence of the paid surface —
 * the same `@/ee-active.generated` barrel that carries the other Team sections is
 * empty in the OSS build. On the free tier the field renders read-only with a hint
 * to upgrade; on Team it is editable with a Save button.
 */

import React, { useCallback, useEffect, useState } from 'react';
import { useDecisionTimeoutQuery, useSetDecisionTimeoutMutation } from '@/hooks/queries/useDecisionTimeout';
import { paidSurface } from '@/ee-active.generated';
import { Clock, Check, Loader2, Lock } from '@/utils/icons';

const DEFAULT_SECONDS = 18000;
const MIN_SECONDS = 60;
const MAX_SECONDS = 1209600; // 14 days

// Team build iff the paid surface has any sections wired in.
const IS_TEAM = Object.keys(paidSurface).length > 0;

function secondsToHours(s: number): string {
  const h = s / 3600;
  return Number.isInteger(h) ? String(h) : h.toFixed(2);
}

export function DecisionTimeoutSection() {
  const { data, isLoading } = useDecisionTimeoutQuery();
  const setTimeoutMutation = useSetDecisionTimeoutMutation();

  const seconds = data?.decision_timeout_seconds ?? DEFAULT_SECONDS;
  const [draftHours, setDraftHours] = useState<string>(secondsToHours(DEFAULT_SECONDS));
  const [saved, setSaved] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  // Sync draft with server value once (and on refetch when the user isn't editing).
  useEffect(() => {
    if (!isLoading) setDraftHours(secondsToHours(seconds));
  }, [seconds, isLoading]);

  const onSave = useCallback(async () => {
    setError(null);
    const hours = Number(draftHours);
    if (!Number.isFinite(hours) || hours <= 0) {
      setError('Enter a positive number of hours.');
      return;
    }
    const nextSeconds = Math.round(hours * 3600);
    if (nextSeconds < MIN_SECONDS || nextSeconds > MAX_SECONDS) {
      setError(`Must be between ${MIN_SECONDS / 60} minutes and ${MAX_SECONDS / 86400} days.`);
      return;
    }
    try {
      await setTimeoutMutation.mutateAsync(nextSeconds);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch {
      setError('Could not save. Please try again.');
    }
  }, [draftHours, setTimeoutMutation]);

  const loading = isLoading;
  const saving = setTimeoutMutation.isPending;

  return (
    <div className="settings-section">
      <div className="settings-section-header">
        <Clock size={18} />
        <h3>Decision Timeout</h3>
      </div>

      <p className="settings-section-desc">
        When a task fails it pauses and waits for a decision (Skip, Mark Success,
        Fail, or Restart) before giving up. This timeout applies to every pipeline.
      </p>

      {loading ? (
        <div className="settings-loading"><Loader2 size={16} className="spin" /> Loading…</div>
      ) : (
        <div className="settings-field-row">
          <label htmlFor="decision-timeout-hours">Wait for</label>
          <input
            id="decision-timeout-hours"
            type="number"
            min="0"
            step="0.5"
            value={draftHours}
            disabled={!IS_TEAM || saving}
            onChange={(e) => setDraftHours(e.target.value)}
            aria-readonly={!IS_TEAM}
          />
          <span className="settings-field-unit">hours</span>

          {IS_TEAM ? (
            <button
              type="button"
              className="settings-save-btn"
              onClick={onSave}
              disabled={saving}
            >
              {saving ? <Loader2 size={14} className="spin" /> : saved ? <Check size={14} /> : 'Save'}
            </button>
          ) : (
            <span className="settings-readonly-hint">
              <Lock size={12} /> Read-only
            </span>
          )}
        </div>
      )}

      {error && <div className="settings-error">{error}</div>}
      {!loading && (
        <p className="settings-current-hint">
          Currently {secondsToHours(seconds)} hours ({seconds.toLocaleString()} seconds).
        </p>
      )}
    </div>
  );
}
