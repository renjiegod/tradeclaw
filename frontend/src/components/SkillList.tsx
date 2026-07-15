import React from "react";
import { Button, Dropdown, Empty, Input, List, Modal, Switch, Tag, message } from "antd";
import { PlusOutlined, MoreOutlined } from "@ant-design/icons";

import type { Skill } from "../types";

type Props = {
  skills: Skill[];
  selectedId: string | null;
  onSelect: (folder: string) => void;
  onToggleEnable: (folder: string, enabled: boolean) => Promise<void>;
  onCreate: (body: { folder_name: string; name: string; description: string }) => Promise<string>;
  onRenameFolder: (folder: string, newName: string) => Promise<string>;
  onDelete: (folder: string) => Promise<void>;
};

export default function SkillList({
  skills, selectedId, onSelect, onToggleEnable, onCreate, onRenameFolder, onDelete,
}: Props) {
  const [q, setQ] = React.useState("");

  const filtered = React.useMemo(() => {
    const lower = q.toLowerCase();
    if (!lower) return skills;
    return skills.filter((s) =>
      s.folder_name.toLowerCase().includes(lower) ||
      s.frontmatter.name.toLowerCase().includes(lower) ||
      s.frontmatter.description.toLowerCase().includes(lower)
    );
  }, [skills, q]);

  const promptCreate = () => {
    // eslint-disable-next-line prefer-const
    let folder = "", name = "", desc = "";
    Modal.confirm({
      title: "新建 skill",
      content: (
        <div>
          <Input placeholder="folder_name (a-z, 0-9, -, _)" onChange={(e) => (folder = e.target.value)} />
          <Input placeholder="name (frontmatter)" onChange={(e) => (name = e.target.value)} style={{ marginTop: 8 }} />
          <Input.TextArea placeholder="description" rows={2} onChange={(e) => (desc = e.target.value)} style={{ marginTop: 8 }} />
        </div>
      ),
      onOk: async () => {
        if (!folder || !name || !desc) {
          message.error("缺少必填字段");
          throw new Error("missing");
        }
        await onCreate({ folder_name: folder, name, description: desc });
      },
    });
  };

  const promptRename = (skill: Skill) => {
    // eslint-disable-next-line prefer-const
    let val = skill.folder_name;
    Modal.confirm({
      title: `重命名文件夹：${skill.folder_name}`,
      content: <Input defaultValue={skill.folder_name} onChange={(e) => (val = e.target.value)} />,
      onOk: async () => {
        if (!val || val === skill.folder_name) return;
        await onRenameFolder(skill.folder_name, val);
      },
    });
  };

  const promptDelete = (skill: Skill) => {
    // eslint-disable-next-line prefer-const
    let typed = "";
    Modal.confirm({
      title: `删除 skill：${skill.folder_name}`,
      content: (
        <div>
          <p>输入文件夹名 <b>{skill.folder_name}</b> 确认删除：</p>
          <Input onChange={(e) => (typed = e.target.value)} />
        </div>
      ),
      okType: "danger",
      onOk: async () => {
        if (typed !== skill.folder_name) {
          message.error("名称不一致，已取消");
          throw new Error("mismatch");
        }
        await onDelete(skill.folder_name);
      },
    });
  };

  return (
    <div>
      <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
        <Input.Search placeholder="搜索" value={q} onChange={(e) => setQ(e.target.value)} />
        <Button icon={<PlusOutlined />} onClick={promptCreate}>新建</Button>
      </div>
      <List
        dataSource={filtered}
        locale={{
          emptyText: (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                skills.length === 0
                  ? "还没有 skill，点「新建」创建第一个"
                  : `没有匹配「${q}」的 skill`
              }
            />
          ),
        }}
        renderItem={(s) => (
          <List.Item
            key={s.folder_name}
            onClick={() => onSelect(s.folder_name)}
            style={{
              cursor: "pointer",
              background: selectedId === s.folder_name ? "rgba(0,0,0,0.04)" : undefined,
              padding: "8px 12px",
            }}
            actions={[
              <Switch
                size="small"
                checked={s.enabled}
                onClick={(_v, e) => e.stopPropagation()}
                onChange={(v) => onToggleEnable(s.folder_name, v)}
              />,
              <Dropdown
                menu={{
                  items: [
                    { key: "rename", label: "重命名文件夹", onClick: () => promptRename(s) },
                    { key: "delete", label: "删除 skill", danger: true, onClick: () => promptDelete(s) },
                  ],
                }}
              >
                <MoreOutlined onClick={(e) => e.stopPropagation()} />
              </Dropdown>,
            ]}
          >
            <List.Item.Meta
              title={<span>{s.folder_name} {s.locked && <Tag color="orange">locked</Tag>}</span>}
              description={s.frontmatter.description}
            />
          </List.Item>
        )}
      />
    </div>
  );
}
