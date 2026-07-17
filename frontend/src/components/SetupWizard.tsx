import { RocketOutlined } from "@ant-design/icons";
import { Alert, Button, Form, Input, Select, Spin, Typography } from "antd";
import { useEffect, useState } from "react";

import { completeSetup, getSetupProviders } from "../api";
import type { SetupProvider } from "../types";

// ---------------------------------------------------------------------------
// Web first-run setup wizard: a full-screen overlay that replaces the terminal
// onboarding prompt (doyoutrade/onboarding.py) when DoYouTrade is launched by
// double-clicking 启动DoYouTrade.bat rather than from a terminal — there is no
// console to type a provider + API key into, so the web console asks instead.
//
// Providers come from GET /setup/providers, which serializes the *same*
// preset list the terminal wizard uses (doyoutrade/onboarding.py::PRESETS) —
// this component must never hardcode its own provider catalog, or the two
// surfaces will drift.
//
// "Skip" only hides this overlay for the current browser (localStorage flag);
// whether DoYouTrade is actually configured is always re-derived from
// GET /setup/status by the caller (App.tsx), never assumed client-side.
// ---------------------------------------------------------------------------

export const SETUP_WIZARD_SKIPPED_KEY = "doyoutrade_setup_wizard_skipped";

type SetupFormValues = {
  provider_index: number;
  api_key: string;
  base_url: string;
  target_model: string;
};

type SetupWizardProps = {
  /** Called once a model route was created + bound successfully. */
  onCompleted: () => void;
  /** Called when the user explicitly skips (sets the localStorage flag). */
  onSkip: () => void;
};

export function SetupWizard({ onCompleted, onSkip }: SetupWizardProps) {
  const [providers, setProviders] = useState<SetupProvider[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [form] = Form.useForm<SetupFormValues>();

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await getSetupProviders();
        if (cancelled) return;
        setProviders(res.items);
        if (res.items.length > 0) {
          applyPresetDefaults(res.items[0], 0);
        }
      } catch (e: unknown) {
        if (cancelled) return;
        setLoadError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const applyPresetDefaults = (preset: SetupProvider, index: number) => {
    form.setFieldsValue({
      provider_index: index,
      base_url: preset.base_url ?? "",
      target_model: preset.model_hint ?? "",
      api_key: "",
    });
  };

  const handleProviderChange = (index: number) => {
    if (!providers) return;
    const preset = providers[index];
    if (preset) applyPresetDefaults(preset, index);
  };

  const handleSkip = () => {
    localStorage.setItem(SETUP_WIZARD_SKIPPED_KEY, "1");
    onSkip();
  };

  const handleSubmit = async () => {
    if (!providers) return;
    try {
      const values = await form.validateFields();
      const preset = providers[values.provider_index];
      if (!preset) {
        setSubmitError("请先选择供应商");
        return;
      }
      setSubmitError(null);
      setSubmitting(true);
      await completeSetup({
        provider_kind: preset.provider_kind,
        api_key: values.api_key.trim(),
        base_url: values.base_url.trim() || null,
        target_model: values.target_model.trim() || null,
      });
      onCompleted();
    } catch (e: unknown) {
      if (e && typeof e === "object" && "errorFields" in e) {
        return;
      }
      setSubmitError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm">
      <div className="w-full max-w-lg rounded-2xl border border-shell-line bg-card-bg p-6 shadow-2xl">
        <div className="mb-4 flex items-center gap-2">
          <RocketOutlined className="text-lg text-shell-accent" />
          <Typography.Title level={4} className="!mb-0">
            欢迎使用 DoYouTrade
          </Typography.Title>
        </div>
        <Typography.Paragraph type="secondary" className="!mb-4 text-sm">
          还没有配置可用的大模型，先选一个供应商填上 API Key，就能开始和智能助手对话了。也可以先跳过，稍后在「设置 →
          模型配置」里再配置。
        </Typography.Paragraph>

        {loadError ? (
          <Alert
            className="mb-4 rounded-xl"
            type="error"
            showIcon
            message="加载供应商列表失败"
            description={loadError}
          />
        ) : null}

        {!providers && !loadError ? (
          <div className="flex justify-center py-8">
            <Spin />
          </div>
        ) : null}

        {providers ? (
          <Form
            form={form}
            layout="vertical"
            initialValues={{ provider_index: 0, api_key: "", base_url: "", target_model: "" }}
            onFinish={() => void handleSubmit()}
          >
            <Form.Item name="provider_index" label="供应商" rules={[{ required: true }]}>
              <Select
                options={providers.map((p, i) => ({ value: i, label: p.label }))}
                onChange={(value: number) => handleProviderChange(value)}
              />
            </Form.Item>
            <Form.Item
              name="api_key"
              label="API Key"
              rules={[
                {
                  validator: async (_rule, value: string) => {
                    const index = form.getFieldValue("provider_index") as number;
                    const preset = providers[index];
                    if (preset?.needs_key && !String(value || "").trim()) {
                      throw new Error("该供应商需要填写 API Key");
                    }
                  },
                },
              ]}
            >
              <Input.Password placeholder="留空表示本地服务无需鉴权" autoComplete="new-password" />
            </Form.Item>
            <Form.Item name="base_url" label="接口地址">
              <Input placeholder="https://…" allowClear />
            </Form.Item>
            <Form.Item name="target_model" label="模型 ID" rules={[{ required: true, message: "请填写模型 ID" }]}>
              <Input placeholder="例如 deepseek-chat" allowClear />
            </Form.Item>

            {submitError ? (
              <Alert className="mb-4 rounded-xl" type="error" showIcon message={submitError} />
            ) : null}

            <div className="mt-2 flex items-center justify-between gap-2">
              <Button type="link" className="!px-0" onClick={handleSkip} disabled={submitting}>
                跳过，稍后在设置里配置
              </Button>
              <Button type="primary" htmlType="submit" className="rounded-xl" loading={submitting}>
                开始使用
              </Button>
            </div>
          </Form>
        ) : null}

        {loadError ? (
          <div className="mt-4 flex justify-end">
            <Button onClick={handleSkip}>跳过，稍后在设置里配置</Button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
