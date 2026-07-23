/** Shared surface chrome reused across cards, panels and tags.
 *
 * These were duplicated verbatim across five components (the two task tables
 * plus ApprovalQueueCard / CreateAgentCard / TaskTriggersPanel /
 * TaskDetailPage); keep the single source here so a theme tweak cannot silently
 * drift between surfaces. */
export const PANEL_CARD_CLASSNAME =
  "!overflow-hidden !border !border-shell-line !bg-card-bg shadow-shell-card";
export const SOFT_TAG_CLASSNAME =
  "!border-soft-tag-border !bg-soft-tag-bg !text-soft-tag-text";

const MODEL_INVOCATION_BASE_CLASSNAME =
  "max-w-none break-words text-sm leading-[1.6] text-shell-ink [&_a]:text-soft-tag-text [&_a:hover]:text-shell-accent [&_blockquote]:border-l-shell-accent [&_blockquote]:text-shell-muted [&_code]:whitespace-normal [&_code]:break-words [&_li]:break-words [&_p]:break-words [&_pre]:max-w-full [&_pre]:overflow-x-auto";

const MODEL_INVOCATION_TABLE_CLASSNAME =
  "[&_table]:w-full [&_table]:border-collapse [&_th]:border [&_th]:border-shell-line [&_th]:px-2 [&_th]:py-1 [&_td]:border [&_td]:border-shell-line [&_td]:px-2 [&_td]:py-1 [&_tbody_tr:nth-child(even)_td]:bg-shell-line/15 [&_th]:bg-shell-line/45";

export const MODEL_INVOCATION_TEXT_CLASSNAME = `${MODEL_INVOCATION_BASE_CLASSNAME} ${MODEL_INVOCATION_TABLE_CLASSNAME}`;

export const MODEL_INVOCATION_PROSE_CLASSNAME = `prose prose-sm ${MODEL_INVOCATION_TEXT_CLASSNAME}`;
