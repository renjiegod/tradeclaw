import React from "react";

import type { ToolCallEntry } from "./types";
import { InlineToolCallCard } from "./InlineToolCallCard";

interface InlineToolCallListProps {
  entries: ToolCallEntry[];
}

export function InlineToolCallList({ entries }: InlineToolCallListProps) {
  if (entries.length === 0) {
    return null;
  }
  return (
    <div className="flex flex-col gap-2">
      {entries.map((entry) => (
        <InlineToolCallCard
          key={entry.tool.id}
          tool={entry.tool}
          result={entry.result}
        />
      ))}
    </div>
  );
}
