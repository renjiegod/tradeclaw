import { ReloadOutlined } from "@ant-design/icons";
import { Button, Card, Empty, List, Spin, Typography, message } from "antd";
import { useCallback, useEffect, useState } from "react";

import { getKnowledgeJournal, listKnowledgeJournals } from "../api";
import type { KnowledgeJournal, KnowledgeJournalListItem } from "../types";
import { formatDateTimeUtc8 } from "../utils/datetime";
import MarkdownPreview from "./MarkdownPreview";

const EMPTY_HINT = "还没有复盘日记 —— 在对话里让 agent 记一次复盘即可";

/**
 * 任务详情页「复盘」Tab 的全局板块：渲染用户私有知识库里的复盘日记
 * （``GET /knowledge/journals`` / ``GET /knowledge/journal``，只读）。
 *
 * 这是用户全局的复盘记录，不绑定到当前任务，因此自取数据、不接收 props。
 * 左侧列表按最新优先排列；选中后右侧用 {@link MarkdownPreview} 渲染正文，
 * 并剥离 YAML frontmatter 以免原样显示。
 */
export function KnowledgeJournalsPanel() {
  const [items, setItems] = useState<KnowledgeJournalListItem[]>([]);
  const [rootExists, setRootExists] = useState(true);
  const [listLoading, setListLoading] = useState(true);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [detail, setDetail] = useState<KnowledgeJournal | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const loadList = useCallback(async () => {
    setListLoading(true);
    try {
      const res = await listKnowledgeJournals();
      setItems(res.items);
      setRootExists(res.root_exists);
      // Auto-select the newest entry (items are returned newest-first).
      setSelectedPath(res.items[0]?.path ?? null);
    } finally {
      setListLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadList().catch((error: unknown) => {
      const detailMsg = error instanceof Error ? error.message : String(error);
      message.error(`加载复盘日记失败：${detailMsg}`);
    });
  }, [loadList]);

  useEffect(() => {
    if (!selectedPath) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setDetail(null);
    setDetailLoading(true);
    void getKnowledgeJournal(selectedPath)
      .then((data) => {
        if (!cancelled) setDetail(data);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const detailMsg = error instanceof Error ? error.message : String(error);
        message.error(`加载复盘日记内容失败：${detailMsg}`);
        setDetail(null);
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedPath]);

  const showEmpty = !listLoading && (!rootExists || items.length === 0);

  return (
    <Card
      className="!border !border-shell-line !bg-card-bg shadow-shell-card"
      title={
        <div className="flex flex-col">
          <Typography.Text strong>个人复盘日记</Typography.Text>
          <Typography.Text type="secondary" className="!text-xs !font-normal">
            来自你的全局知识库（不限于本任务）的复盘记录，只读。
          </Typography.Text>
        </div>
      }
      extra={
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={listLoading}
          onClick={() =>
            void loadList().catch((error: unknown) => {
              const detailMsg = error instanceof Error ? error.message : String(error);
              message.error(`加载复盘日记失败：${detailMsg}`);
            })
          }
        >
          刷新
        </Button>
      }
      data-testid="knowledge-journals-panel"
    >
      {listLoading ? (
        <div className="flex min-h-[200px] items-center justify-center">
          <Spin />
        </div>
      ) : showEmpty ? (
        <Empty description={EMPTY_HINT} image={Empty.PRESENTED_IMAGE_SIMPLE} />
      ) : (
        <div className="flex flex-col gap-4 md:flex-row">
          <div className="shrink-0 md:w-[260px]">
            <List<KnowledgeJournalListItem>
              size="small"
              bordered
              className="!rounded-xl"
              dataSource={items}
              rowKey={(item) => item.path}
              renderItem={(item) => {
                const active = item.path === selectedPath;
                return (
                  <List.Item
                    onClick={() => setSelectedPath(item.path)}
                    className={`!cursor-pointer ${active ? "!bg-soft-tag-bg" : ""}`}
                    data-testid="knowledge-journal-list-item"
                  >
                    <div className="flex w-full flex-col">
                      <Typography.Text strong={active}>{item.title}</Typography.Text>
                      <Typography.Text type="secondary" className="!text-xs">
                        {formatDateTimeUtc8(item.mtime)}
                      </Typography.Text>
                    </div>
                  </List.Item>
                );
              }}
            />
          </div>
          <div className="min-w-0 flex-1">
            {detailLoading ? (
              <div className="flex min-h-[200px] items-center justify-center">
                <Spin />
              </div>
            ) : detail ? (
              <div data-testid="knowledge-journal-content">
                <MarkdownPreview source={detail.content} stripFrontmatter />
              </div>
            ) : (
              <div className="flex min-h-[200px] items-center justify-center">
                <Typography.Text type="secondary">选择左侧一篇复盘日记查看内容。</Typography.Text>
              </div>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}
