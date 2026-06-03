import { Loader2 } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";

import { useReport } from "../hooks/useReports";
import { parseFrontmatter } from "../lib/utils";

const LANG_LABEL: Record<string, string> = {
  "zh-Hans": "中文 (zh-Hans)",
  en: "English",
  ja: "日本語",
  ko: "한국어",
};

export function ReportViewer({ filename }: { filename: string | undefined }) {
  const { data, isLoading } = useReport(filename);

  if (!filename) {
    return (
      <div className="flex h-full items-center justify-center px-8 text-center">
        <div className="text-[12.5px] text-[var(--color-fg-tertiary)]">
          Select a report from the list.
        </div>
      </div>
    );
  }
  if (isLoading || !data) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-[12px] text-[var(--color-fg-tertiary)]">
        <Loader2 size={13} className="animate-spin" /> Loading {filename}…
      </div>
    );
  }
  const { meta, body } = parseFrontmatter(data);

  const stats = [
    { k: "Student", v: meta.student },
    { k: "Language", v: LANG_LABEL[meta.feedback_language] ?? meta.feedback_language },
    { k: "Generated", v: meta.generated_at },
    { k: "Submissions", v: meta.submission_count },
    { k: "Grammar", v: meta.grammar_training_count },
    { k: "Learning Needs", v: meta.learning_need_count },
  ].filter((s) => s.v !== undefined && s.v !== "");

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-[760px] px-9 pb-20 pt-9">
        {/* Hero title from meta */}
        <div className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.08em] text-[var(--color-fg-tertiary)]">
          Student Learning Profile
        </div>
        <h1 className="text-[28px] font-bold leading-tight tracking-[-0.022em] text-[var(--color-fg)]">
          {meta.student ?? filename}
        </h1>
        <div className="mt-1.5 text-[12.5px] text-[var(--color-fg-secondary)]">
          {LANG_LABEL[meta.feedback_language] ?? meta.feedback_language}
          {meta.generated_at && (
            <>
              <span className="mx-1.5 text-[var(--color-fg-quaternary)]">·</span>
              <span>Generated {meta.generated_at}</span>
            </>
          )}
        </div>

        {/* Stat grid card */}
        {stats.length > 0 && (
          <div
            className="mt-5 grid grid-cols-3 gap-x-7 gap-y-3.5 rounded-[14px] px-5 py-4"
            style={{
              background: "var(--color-surface-2)",
              boxShadow: "var(--shadow-hairline)",
            }}
          >
            {stats.map((s) => (
              <div key={s.k}>
                <div className="text-[10px] font-semibold uppercase tracking-[0.07em] text-[var(--color-fg-tertiary)]">
                  {s.k}
                </div>
                <div className="mt-0.5 text-[13.5px] font-semibold tabular-nums text-[var(--color-fg)]">
                  {s.v}
                </div>
              </div>
            ))}
          </div>
        )}

        {/* Markdown body — skip the first H1/Report Info since we render them above */}
        <article className="prose-report mt-7">
          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
            {stripIntro(body)}
          </ReactMarkdown>
        </article>
      </div>
    </div>
  );
}

/** Hide the first H1 + Report Info table since we render them as the hero above. */
function stripIntro(md: string): string {
  let out = md;
  out = out.replace(/^\s*#\s+Student Learning Profile\s*\n+/m, "");
  out = out.replace(/^##\s+Report Info[\s\S]*?(?=^##\s+)/m, "");
  return out;
}
