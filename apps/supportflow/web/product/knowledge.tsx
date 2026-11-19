import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Button } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, describeError, SessionApi } from "./api";
import type {
  Actor, DocumentActivationInput, DocumentActivationResult, DocumentStaged, DocumentsPage, DocumentVersionsPage,
} from "./contracts";

type Document = DocumentsPage["items"][number];
type Version = DocumentVersionsPage["items"][number];
type Visibility = Document["visibility"];

const listKey = ["knowledge", "documents"];
const versionKey = (id: string) => ["knowledge", "versions", id];
const versionPath = (id: string) => "/documents/" + encodeURIComponent(id) + "/versions";
const uploadLimit = 2 * 1024 * 1024;
const indexLabels: Record<Version["index_state"], string> = {
  staged: "待发布", building: "构建中", ready: "等待发布", active: "有效版本",
  retired: "历史版本", failed: "导入失败", interrupted: "构建中断",
};
const failureLabels: Record<string, string> = {
  DOCUMENT_PARSE_FAILED: "正文解析失败，请检查文档后上传新版本。",
  DUPLICATE_SECTION: "文档包含重复的章节标识，请修改后上传新版本。",
  EMPTY_INDEX: "文件没有可索引的正文。",
  INVALID_TOKENIZATION: "正文分块失败，请修改后上传新版本。",
  INDEX_BUILD_INTERRUPTED: "索引构建中断，可以重新发布。",
  INDEX_BUILD_FAILED: "索引构建失败，可以重新发布。",
  EMBEDDING_UNAVAILABLE: "本地向量模型暂时不可用。",
  GENERATION_CONFLICT: "有效索引已更新，请查看最新版本后重试。",
  INGEST_BUSY: "当前有索引正在构建，请稍后刷新。",
  RESULT_TOO_LARGE: "知识文件最多 2 MiB。",
  INVALID_DOCUMENT: "请选择非空 UTF-8 编码的 MD 或 TXT 文件。",
};

function knowledgeError(error: unknown) {
  if (error instanceof ApiError && error.status === 404) return "知识文档或版本不可见。";
  if (error instanceof ApiError && failureLabels[error.code]) return failureLabels[error.code];
  return describeError(error);
}

function KnowledgeDialog({ label, drawer = false, busy, onClose, children }: {
  label: string; drawer?: boolean; busy: boolean; onClose: () => void; children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return <dialog ref={ref} className={drawer ? "drawer" : "modal"} aria-label={label}
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <div className="dialog-top"><span>{label}</span><button type="button" aria-label="关闭"
      className="close-button" onClick={onClose} disabled={busy}>×</button></div>
    {children}
  </dialog>;
}

function UploadKnowledge({ api, document, onClose, onComplete }: {
  api: SessionApi; document: Document | null; onClose: () => void; onComplete: (result: DocumentStaged) => void;
}) {
  const [title, setTitle] = useState(document?.title ?? "");
  const [visibility, setVisibility] = useState<Visibility>(document?.visibility ?? "tenant");
  const [file, setFile] = useState<File>();
  const [error, setError] = useState("");
  const intent = useRef<{ file: File; title: string; visibility: Visibility; key: string } | null>(null);
  const submitting = useRef(false);
  const mutation = useMutation({ mutationFn: ({ body, key }: { body: FormData; key: string }) =>
    api.upload<DocumentStaged>("/documents", body, key) });

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current) return;
    if (!file || !title.trim()) { setError("请填写文档标题并选择文件。"); return; }
    if (file.size > uploadLimit) { setError(failureLabels.RESULT_TOO_LARGE); return; }
    if (!/\.(md|txt)$/i.test(file.name)) { setError(failureLabels.INVALID_DOCUMENT); return; }
    const cleanTitle = title.trim();
    if (intent.current?.file !== file || intent.current.title !== cleanTitle || intent.current.visibility !== visibility) {
      intent.current = { file, title: cleanTitle, visibility, key: crypto.randomUUID() };
    }
    const body = new FormData();
    body.set("file", file);
    body.set("title", cleanTitle);
    body.set("visibility", visibility);
    if (document) body.set("document_id", document.document_id);
    submitting.current = true;
    setError("");
    try { onComplete(await mutation.mutateAsync({ body, key: intent.current.key })); }
    catch (failure) { setError(knowledgeError(failure)); }
    finally { submitting.current = false; }
  }

  return <KnowledgeDialog label={document ? "上传新版本" : "上传知识"} busy={mutation.isPending} onClose={onClose}>
    <form className="dialog-body new-form" onSubmit={submit}>
      <label>文档标题<input value={title} maxLength={200} required readOnly={Boolean(document)}
        disabled={mutation.isPending} onChange={event => setTitle(event.target.value)}/></label>
      <label>知识文件<input type="file" accept=".md,.txt" required disabled={mutation.isPending}
        onChange={event => setFile(event.target.files?.[0])}/></label>
      <label>可见范围<select value={visibility} disabled={Boolean(document) || mutation.isPending}
        onChange={event => setVisibility(event.target.value as Visibility)}>
        <option value="tenant">租户可见</option><option value="private">仅授权成员可见</option>
      </select></label>
      <p className="intro">UTF-8 编码的 MD 或 TXT，最多 2 MiB。</p>
      {error && <p role="alert" className="error">{error}</p>}
      <div className="actions"><button type="button" disabled={mutation.isPending} onClick={onClose}>取消</button>
        <Button type="primary" htmlType="submit" loading={mutation.isPending}>上传文档</Button></div>
    </form>
  </KnowledgeDialog>;
}

function Versions({ api, actor, id, document, onClose, onUpload }: {
  api: SessionApi; actor: Actor; id: string; document: Document | undefined;
  onClose: () => void; onUpload: (document: Document) => void;
}) {
  const cache = useQueryClient();
  const [notice, setNotice] = useState("");
  const activating = useRef(false);
  const versions = useQuery({ queryKey: versionKey(id),
    queryFn: ({ signal }) => api.get<DocumentVersionsPage>(versionPath(id), signal),
    refetchInterval: query => query.state.data?.items.some(item => item.index_state === "building" || item.index_state === "ready")
      ? 2000 : false });
  const mutation = useMutation({ mutationFn: ({ versionId, body }: { versionId: string; body: DocumentActivationInput }) =>
    api.post<DocumentActivationResult>(versionPath(id) + "/" + encodeURIComponent(versionId) + "/activate", body) });

  async function activate(version: Version) {
    if (activating.current || !versions.data || versions.isError) return;
    activating.current = true;
    setNotice("");
    try {
      const result = await mutation.mutateAsync({ versionId: version.version_id,
        body: { expected_generation: versions.data.active_generation } });
      setNotice(result.status === "active" ? "版本已发布。" : "索引正在构建，页面会更新状态。");
    } catch { /* Render the mutation's sanitized error below. */ }
    finally {
      await cache.invalidateQueries({ queryKey: ["knowledge"] });
      activating.current = false;
    }
  }

  return <KnowledgeDialog label="知识版本" drawer busy={mutation.isPending} onClose={onClose}>
    <div className="dialog-body">
      {versions.isPending ? <p role="status">正在加载知识版本…</p> : versions.isError ?
        <><p role="alert" className="error">{knowledgeError(versions.error)}</p>
          <button onClick={() => void versions.refetch()}>重新加载</button></> : <>
          {document && <h2>{document.title}</h2>}
          <div className="actions">
            <button disabled={versions.isFetching || mutation.isPending} onClick={() => void versions.refetch()}>刷新版本</button>
            {actor.role === "admin" && document && <Button disabled={mutation.isPending} onClick={() => onUpload(document)}>上传新版本</Button>}
          </div>
          {mutation.isPending && <p role="status">正在构建索引…</p>}
          {notice && <p role="status">{notice}</p>}
          {mutation.isError && <p className="error" role="alert">{knowledgeError(mutation.error)}</p>}
          {!versions.data.items.length && <p>暂无版本。</p>}
          {versions.data.items.map(version => <section className="threads" aria-label={"版本 " + version.version_id} key={version.version_id}>
            <h3><span className="status">{indexLabels[version.index_state]}</span></h3>
            <code>{version.version_id}</code>
            <p>{new Date(version.created_at).toLocaleString("zh-CN")}{version.index_generation !== null && ` · 索引代 ${version.index_generation}`}</p>
            {version.failure_code && <p className="error">{failureLabels[version.failure_code] ?? "索引暂时不可用。"}</p>}
            {actor.role === "admin" && version.index_state !== "active" && <Button
              disabled={mutation.isPending || versions.isFetching || version.parse_status === "failed"
                || version.index_state === "building" || version.index_state === "ready"}
              onClick={() => void activate(version)}>发布版本</Button>}
          </section>)}
        </>}
    </div>
  </KnowledgeDialog>;
}

export function Knowledge({ api, actor }: { api: SessionApi; actor: Actor }) {
  const cache = useQueryClient();
  const [selected, setSelected] = useState<string>();
  const [upload, setUpload] = useState<Document | null>();
  const documents = useQuery({ queryKey: listKey,
    queryFn: ({ signal }) => api.get<DocumentsPage>("/documents", signal) });
  const selectedDocument = documents.data?.items.find(document => document.document_id === selected);
  function uploaded(result: DocumentStaged) {
    setUpload(undefined);
    setSelected(result.document_id);
    void cache.invalidateQueries({ queryKey: ["knowledge"] });
  }
  return <main className="tickets-page wrap">
    <div className="page-heading"><h1>知识</h1>
      {actor.role === "admin" && <Button type="primary" onClick={() => setUpload(null)}>上传知识</Button>}</div>
    <section className="ticket-panel" aria-label="知识文档">
      {documents.isPending ? <p className="empty" role="status">正在加载知识…</p> : documents.isError ?
        <div className="empty"><p role="alert">{knowledgeError(documents.error)}</p>
          <button onClick={() => void documents.refetch()}>重新加载</button></div> : documents.data.items.length === 0 ?
          <p className="empty">暂无可见知识文档。</p> : <table>
            <thead><tr><th scope="col">文档</th><th scope="col">可见范围</th><th scope="col">状态</th></tr></thead>
            <tbody>{documents.data.items.map(document => <tr key={document.document_id}>
              <td><button className="ticket-link" onClick={() => setSelected(document.document_id)}>{document.title}</button></td>
              <td>{document.visibility === "tenant" ? "租户" : "私有"}</td>
              <td>{document.active_version_id ? "已发布" : "待发布"}</td>
            </tr>)}</tbody>
          </table>}
    </section>
    {upload !== undefined ? <UploadKnowledge api={api} document={upload} onComplete={uploaded} onClose={() => setUpload(undefined)}/> :
      selected && <Versions key={selected} api={api} actor={actor} id={selected} document={selectedDocument}
        onClose={() => setSelected(undefined)} onUpload={setUpload}/>}
  </main>;
}
