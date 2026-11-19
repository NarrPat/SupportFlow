import type { Citation, EvidenceRef } from "./contracts";

export type CitationState = { ref: EvidenceRef | null; loading: boolean; value: Citation | null; error: string | null };
export type CitationLoader = (path: string, signal: AbortSignal) => Promise<Citation>;
const empty = (): CitationState => ({ ref: null, loading: false, value: null, error: null });
const fields: Array<keyof EvidenceRef> = ["evidence_id", "document_id", "version_id", "section_id", "generation",
  "title", "source", "start_line", "end_line", "content_sha256"];

/** Each open reads current authorization; a closed or superseded request cannot render. */
export class CitationClient {
  private request: AbortController | null = null;
  private sequence = 0;
  constructor(private load: CitationLoader, private changed: (state: CitationState) => void) {}

  async open(ref: EvidenceRef): Promise<void> {
    this.request?.abort();
    const controller = new AbortController();
    this.request = controller;
    const sequence = ++this.sequence;
    this.changed({ ref, loading: true, value: null, error: null });
    try {
      const value = await this.load(`/citations/${encodeURIComponent(ref.evidence_id)}?generation=${ref.generation}`, controller.signal);
      if (controller.signal.aborted || sequence !== this.sequence) return;
      if (fields.some(field => value[field] !== ref[field]) || typeof value.content !== "string") {
        this.changed({ ref, loading: false, value: null, error: "引用内容与本次运行的版本不一致，请重新读取运行记录。" });
        return;
      }
      this.changed({ ref, loading: false, value, error: null });
    } catch (error) {
      if (controller.signal.aborted || sequence !== this.sequence) return;
      const status = typeof error === "object" && error !== null && "status" in error ? error.status : null;
      this.changed({ ref, loading: false, value: null, error: status === 404 || status === 403
        ? "该引用当前不可访问，请重新确认文档权限。" : "引用暂时无法读取，请重新点击查看。" });
    }
  }

  close(): void {
    this.sequence++;
    this.request?.abort();
    this.request = null;
    this.changed(empty());
  }
}
