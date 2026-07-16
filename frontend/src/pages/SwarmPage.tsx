import { useCallback, useEffect, useMemo, useState } from "react";
import { Alert, Button, Card, Empty, Form, Input, Select, Space, Tag, Typography, message } from "antd";

import { listSwarmPresets, startSwarmRun } from "../api";
import { SwarmStatusCard } from "../components/SwarmStatusCard";
import { useSwarmRunStream } from "../hooks/useSwarmRunStream";
import { PANEL_CARD_CLASSNAME } from "../styles/classNames";
import type { SwarmPresetSummary } from "../types";

type PresetVar = { name: string; description?: string; required?: boolean };

function normalizeVars(preset: SwarmPresetSummary | undefined): PresetVar[] {
  if (!preset) return [];
  return preset.variables.map((v) =>
    typeof v === "string" ? { name: v } : { name: v.name, description: v.description, required: v.required },
  );
}

const RUN_STATUS_META: Record<string, { label: string; color: string }> = {
  pending: { label: "待启动", color: "default" },
  running: { label: "运行中", color: "processing" },
  completed: { label: "已完成", color: "success" },
  failed: { label: "失败", color: "error" },
  cancelled: { label: "已取消", color: "default" },
};

export function SwarmPage() {
  const [presets, setPresets] = useState<SwarmPresetSummary[]>([]);
  const [loadingPresets, setLoadingPresets] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [form] = Form.useForm();

  const { run, workerStatus, runStatus } = useSwarmRunStream(activeRunId);

  useEffect(() => {
    let cancelled = false;
    setLoadingPresets(true);
    listSwarmPresets()
      .then((rows) => {
        if (cancelled) return;
        setPresets(rows);
        if (rows.length > 0) setSelected((cur) => cur ?? rows[0].name);
      })
      .catch(() => {
        if (!cancelled) message.error("加载 preset 列表失败");
      })
      .finally(() => {
        if (!cancelled) setLoadingPresets(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const selectedPreset = useMemo(
    () => presets.find((p) => p.name === selected),
    [presets, selected],
  );
  const presetVars = useMemo(() => normalizeVars(selectedPreset), [selectedPreset]);

  const handleStart = useCallback(async () => {
    if (!selected) return;
    try {
      const values = await form.validateFields();
      const userVars: Record<string, string> = {};
      for (const v of presetVars) {
        const val = values[v.name];
        if (val != null && String(val).trim() !== "") userVars[v.name] = String(val).trim();
      }
      setStarting(true);
      const created = await startSwarmRun(selected, userVars);
      setActiveRunId(created.id);
      message.success(`已启动 swarm run：${created.id}`);
    } catch (err) {
      if (err && typeof err === "object" && "errorFields" in err) return; // 表单校验失败
      message.error(`启动失败：${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setStarting(false);
    }
  }, [selected, presetVars, form]);

  const statusMeta = runStatus ? RUN_STATUS_META[runStatus] : null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <Card className={PANEL_CARD_CLASSNAME} title="Swarm" loading={loadingPresets}>
        <Space direction="vertical" size={16} style={{ width: "100%" }}>
          <Form layout="vertical" form={form}>
            <Form.Item label="选择团队（preset）">
              <Select
                value={selected ?? undefined}
                onChange={(v) => {
                  setSelected(v);
                  form.resetFields();
                }}
                options={presets.map((p) => ({
                  value: p.name,
                  label: `${p.title || p.name} · ${p.agent_count} agents`,
                }))}
                placeholder="选择一个 swarm 团队"
              />
            </Form.Item>
            {selectedPreset ? (
              <Typography.Paragraph type="secondary">
                {selectedPreset.description}
              </Typography.Paragraph>
            ) : null}
            {presetVars.map((v) => (
              <Form.Item
                key={v.name}
                name={v.name}
                label={v.name}
                tooltip={v.description}
                rules={v.required ? [{ required: true, message: `请填写 ${v.name}` }] : []}
              >
                <Input placeholder={v.description ?? v.name} />
              </Form.Item>
            ))}
          </Form>
          <Button type="primary" loading={starting} disabled={!selected} onClick={handleStart}>
            启动 Swarm
          </Button>
        </Space>
      </Card>

      {activeRunId ? (
        <>
          <Card
            className={PANEL_CARD_CLASSNAME}
            title={
              <Space>
                <span>运行 {activeRunId}</span>
                {statusMeta ? <Tag color={statusMeta.color}>{statusMeta.label}</Tag> : null}
              </Space>
            }
          >
            {run?.error ? <Alert type="error" showIcon message={run.error} /> : null}
            {run ? (
              <Typography.Text type="secondary">
                tokens：入 {run.total_input_tokens} / 出 {run.total_output_tokens}
              </Typography.Text>
            ) : null}
          </Card>

          <SwarmStatusCard tasks={run?.tasks ?? []} workerStatus={workerStatus} loading={!run} />

          {run?.final_report ? (
            <Card className={PANEL_CARD_CLASSNAME} title="最终报告">
              <Typography.Paragraph style={{ whiteSpace: "pre-wrap" }}>
                {run.final_report}
              </Typography.Paragraph>
            </Card>
          ) : null}
        </>
      ) : (
        <Card className={PANEL_CARD_CLASSNAME}>
          <Empty description="选择一个团队并启动，即可在此查看实时协作状态" />
        </Card>
      )}
    </div>
  );
}
