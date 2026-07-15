import { ReloadOutlined, StarFilled, WarningFilled } from "@ant-design/icons";
import { Alert, Button, Card, Empty, Spin, Table, Tag, Tree, Typography, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { DataNode } from "antd/es/tree";

import { getKnowledgeFile, getKnowledgeIndex } from "../api";
import type { KnowledgeFile, KnowledgeIndex, KnowledgeIndexEntry } from "../types";
import { formatDateTimeUtc8 } from "../utils/datetime";
import MarkdownPreview from "./MarkdownPreview";

const EMPTY_HINT = "知识库还没有任何文件 —— 在对话里让 agent 记一次复盘 / 角色笔记即可";
const KEY_SEP = "|";

/** Encode a selectable file leaf's key as ``<partition>|<rel_path>``. */
function fileKey(partition: string, relPath: string): string {
  return `${partition}${KEY_SEP}${relPath}`;
}

/** Parse a selected leaf key back into ``{partition, path}`` (or ``null``). */
function parseFileKey(key: string): { partition: string; path: string } | null {
  const idx = key.indexOf(KEY_SEP);
  if (idx <= 0) return null;
  return { partition: key.slice(0, idx), path: key.slice(idx + 1) };
}

function entryTitle(entry: KnowledgeIndexEntry): React.ReactNode {
  return (
    <span className="flex items-center gap-1">
      {entry.is_overview && <StarFilled style={{ color: "#faad14", fontSize: 11 }} />}
      {entry.weak && <WarningFilled style={{ color: "#fa8c16", fontSize: 11 }} />}
      <Typography.Text type={entry.weak ? "danger" : undefined} className="!text-[13px]">
        {entry.title}
      </Typography.Text>
    </span>
  );
}

/**
 * Build the antd {@link Tree} data model from the structured knowledge index:
 * partition (file_count) → group (month/year/strategy) → file leaf.
 */
function buildTreeData(index: KnowledgeIndex): DataNode[] {
  return index.partitions
    .filter((p) => p.groups.length > 0)
    .map((partition) => ({
      key: `part:${partition.name}`,
      title: (
        <Typography.Text strong className="!text-[13px]">
          {partition.name}/ <span className="!font-normal !text-xs opacity-70">— {partition.label}（{partition.file_count}）</span>
        </Typography.Text>
      ),
      selectable: false,
      children: partition.groups.map((group) => ({
        key: `group:${partition.name}:${group.name}`,
        title: (
          <Typography.Text type="secondary" className="!text-[12px]">
            {group.name}
          </Typography.Text>
        ),
        selectable: false,
        children: partition.name === "symbols" && group.name === partition.name
          ? group.entries.map((e) => leafNode(partition.name, e))
          : group.entries.map((e) => leafNode(partition.name, e)),
      })),
    }));
}

function leafNode(partition: string, entry: KnowledgeIndexEntry): DataNode {
  return {
    key: fileKey(partition, entry.rel_path),
    title: entryTitle(entry),
    isLeaf: true,
  };
}

/**
 * Top-level Knowledge page panel: a read-only browser over the whole private
 * knowledge base. Left = the {@link getKnowledgeIndex} navigation tree (five
 * partitions, grouped by month / year / strategy, with ⭐ overview + ⚠️ weak
 * markers). Right = the selected file rendered as markdown (via
 * {@link MarkdownPreview}) or an antd Table (CSV broker exports).
 *
 * Read-only by design — writes stay agent-gated (see the doyoutrade-knowledge
 * skill's "Privacy boundary").
 */
export function KnowledgeBrowserPanel() {
  const [index, setIndex] = useState<KnowledgeIndex | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [file, setFile] = useState<KnowledgeFile | null>(null);
  const [fileLoading, setFileLoading] = useState(false);

  const loadIndex = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getKnowledgeIndex();
      setIndex(res);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadIndex().catch((error: unknown) => {
      const msg = error instanceof Error ? error.message : String(error);
      message.error(`加载知识库索引失败：${msg}`);
    });
  }, [loadIndex]);

  useEffect(() => {
    if (!selectedKey) {
      setFile(null);
      return;
    }
    const parsed = parseFileKey(selectedKey);
    if (!parsed) {
      setFile(null);
      return;
    }
    let cancelled = false;
    setFile(null);
    setFileLoading(true);
    void getKnowledgeFile(parsed.partition, parsed.path)
      .then((data) => {
        if (!cancelled) setFile(data);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const msg = error instanceof Error ? error.message : String(error);
        message.error(`加载文件失败：${msg}`);
        setFile(null);
      })
      .finally(() => {
        if (!cancelled) setFileLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedKey]);

  const treeData = useMemo(() => (index ? buildTreeData(index) : []), [index]);
  const showEmpty = !loading && (!index || !index.root_exists || index.total_files === 0);

  return (
    <Card
      className="!border !border-shell-line !bg-card-bg shadow-shell-card"
      title={
        <div className="flex flex-col">
          <Typography.Text strong>知识库</Typography.Text>
          <Typography.Text type="secondary" className="!text-xs !font-normal">
            {index
              ? `共 ${index.total_files} 个文件 · 六分区只读浏览（情绪周期 / 标的角色 / 交割单 / 复盘 / 回测 / 打板模式）`
              : "全库只读浏览器"}
          </Typography.Text>
        </div>
      }
      extra={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={() =>
            void loadIndex().catch((error: unknown) => {
              const msg = error instanceof Error ? error.message : String(error);
              message.error(`加载知识库索引失败：${msg}`);
            })
          }
        >
          刷新
        </Button>
      }
      data-testid="knowledge-browser-panel"
    >
      {index && index.weak_title_count > 0 && (
        <Alert
          type="warning"
          showIcon
          className="!mb-3"
          message={`${index.weak_title_count} 个弱标题文件`}
          description="这些文件缺少 `# ` 标题行（地图里只显示文件名），让 agent 给每篇补一个自描述标题即可。"
          data-testid="knowledge-weak-alert"
        />
      )}

      {loading ? (
        <div className="flex min-h-[260px] items-center justify-center">
          <Spin />
        </div>
      ) : showEmpty ? (
        <Empty description={EMPTY_HINT} image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <div className="flex flex-col gap-4 md:flex-row">
          <div className="shrink-0 md:w-[300px]" data-testid="knowledge-tree">
            <Tree
              treeData={treeData}
              defaultExpandAll
              selectedKeys={selectedKey ? [selectedKey] : []}
              onSelect={(keys) => {
                const k = keys[0];
                setSelectedKey(typeof k === "string" && parseFileKey(k) ? k : null);
              }}
              className="!text-[13px]"
            />
          </div>
          <div className="min-w-0 flex-1">
            {fileLoading ? (
              <div className="flex min-h-[260px] items-center justify-center">
                <Spin />
              </div>
            ) : file ? (
              <FileReader file={file} />
            ) : (
              <div className="flex min-h-[260px] items-center justify-center">
                <Typography.Text type="secondary">
                  选择左侧一个文件查看内容。
                </Typography.Text>
              </div>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}

/** Render a fetched knowledge file: markdown body or a CSV broker-export table. */
function FileReader({ file }: { file: KnowledgeFile }) {
  return (
    <div data-testid="knowledge-file-reader" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Typography.Text strong className="!text-sm">
          {file.title}
        </Typography.Text>
        <Tag className="!m-0">{file.partition}</Tag>
        <Tag className="!m-0">{file.suffix}</Tag>
        <Typography.Text type="secondary" className="!text-xs">
          {formatDateTimeUtc8(file.mtime)} · {(file.size / 1024).toFixed(1)} KB
        </Typography.Text>
      </div>
      <div className="rounded-xl border border-shell-line bg-shell-surface/40 p-3">
        {file.kind === "markdown" ? (
          <div data-testid="knowledge-file-markdown">
            <MarkdownPreview source={file.content} stripFrontmatter />
          </div>
        ) : (
          <CsvTable file={file} />
        )}
      </div>
    </div>
  );
}

/** Render a parsed CSV broker export as a paginated antd Table. */
function CsvTable({ file }: { file: Extract<KnowledgeFile, { kind: "csv" }> }) {
  const columns: ColumnsType<Record<string, string>> = useMemo(
    () =>
      file.columns.map((col) => ({
        title: col,
        dataIndex: col,
        key: col,
        ellipsis: true,
        className: "!whitespace-nowrap",
        render: (v: string) => (
          <Typography.Text className="!text-[12px] font-mono">{v}</Typography.Text>
        ),
      })),
    [file.columns],
  );

  const dataSource = useMemo(
    () =>
      file.rows.map((row, idx) => {
        const obj: Record<string, string> = { _key: String(idx) };
        file.columns.forEach((col, ci) => {
          obj[col] = row[ci] ?? "";
        });
        return obj;
      }),
    [file.rows, file.columns],
  );

  return (
    <div data-testid="knowledge-file-csv">
      {file.truncated && (
        <Alert
          type="info"
          showIcon
          className="!mb-2"
          message={`CSV 已截断：仅显示前 ${file.row_count} 行（交割单可能很大）`}
        />
      )}
      <Table<Record<string, string>>
        size="small"
        rowKey="_key"
        columns={columns}
        dataSource={dataSource}
        pagination={{ pageSize: 50, showSizeChanger: true, pageSizeOptions: ["20", "50", "100", "200"] }}
        scroll={{ x: "max-content" }}
      />
    </div>
  );
}
