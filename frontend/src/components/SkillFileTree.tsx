import React from "react";
import { Button, Modal, Space, Tooltip, Tree, message } from "antd";
import { FileMarkdownOutlined, FileOutlined, FolderOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";

import type { SkillFileNode } from "../types";

type Props = {
  tree: SkillFileNode[];
  selectedPath: string | null;
  onSelect: (path: string) => void;
  onCreate: (parentDir: string, kind: "file" | "dir", name: string) => Promise<void>;
  onRename: (fromPath: string, toPath: string) => Promise<void>;
  onDelete: (path: string) => Promise<void>;
  onRefresh: () => void;
};

function iconFor(node: SkillFileNode) {
  if (node.kind === "dir") return <FolderOutlined />;
  if (node.mime === "text/markdown") return <FileMarkdownOutlined />;
  return <FileOutlined />;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function toTreeNodes(nodes: SkillFileNode[]): any[] {
  return nodes.map((n) => ({
    key: n.path,
    title: n.name,
    icon: iconFor(n),
    isLeaf: n.kind === "file",
    selectable: true,
    children: n.children ? toTreeNodes(n.children) : undefined,
    dataRef: n,
  }));
}

export default function SkillFileTree({
  tree, selectedPath, onSelect, onCreate, onRename, onDelete, onRefresh,
}: Props) {
  const promptCreate = (parentDir: string, kind: "file" | "dir") => {
    let inputName = "";
    Modal.confirm({
      title: kind === "file" ? "新建文件" : "新建文件夹",
      content: (
        <input
          autoFocus
          style={{ width: "100%" }}
          placeholder={kind === "file" ? "name.md" : "subdir"}
          onChange={(e) => { inputName = e.target.value; }}
        />
      ),
      onOk: async () => {
        if (!inputName) return;
        try {
          await onCreate(parentDir, kind, inputName);
        } catch (e) {
          message.error(String((e as Error).message ?? e));
        }
      },
    });
  };

  const handleDelete = (node: SkillFileNode) => {
    if (node.path === "SKILL.md") {
      message.warning("SKILL.md 不可删除");
      return;
    }
    Modal.confirm({
      title: `删除 ${node.path}?`,
      okType: "danger",
      onOk: () => onDelete(node.path),
    });
  };

  const handleRename = (node: SkillFileNode) => {
    if (node.path === "SKILL.md") {
      message.warning("SKILL.md 不可重命名");
      return;
    }
    let toName = node.name;
    Modal.confirm({
      title: `重命名 ${node.path}`,
      content: (
        <input
          autoFocus
          defaultValue={node.name}
          style={{ width: "100%" }}
          onChange={(e) => { toName = e.target.value; }}
        />
      ),
      onOk: async () => {
        if (!toName || toName === node.name) return;
        const parts = node.path.split("/");
        parts[parts.length - 1] = toName;
        await onRename(node.path, parts.join("/"));
      },
    });
  };

  return (
    <div>
      <Space style={{ marginBottom: 8 }}>
        <Tooltip title="新建文件"><Button size="small" icon={<PlusOutlined />} onClick={() => promptCreate("", "file")}>文件</Button></Tooltip>
        <Tooltip title="新建文件夹"><Button size="small" icon={<FolderOutlined />} onClick={() => promptCreate("", "dir")}>目录</Button></Tooltip>
        <Tooltip title="刷新"><Button size="small" icon={<ReloadOutlined />} onClick={onRefresh} /></Tooltip>
      </Space>
      <Tree
        showIcon
        defaultExpandAll
        treeData={toTreeNodes(tree)}
        selectedKeys={selectedPath ? [selectedPath] : []}
        onSelect={(keys, info) => {
          const node = (info.node as any).dataRef as SkillFileNode; // eslint-disable-line @typescript-eslint/no-explicit-any
          if (node.kind === "file") onSelect(node.path);
        }}
        titleRender={(node: any) => { // eslint-disable-line @typescript-eslint/no-explicit-any
          const ref = node.dataRef as SkillFileNode;
          return (
            <span>
              {node.title}
              <span style={{ marginLeft: 8, opacity: 0.5 }}>
                <a onClick={(e) => { e.stopPropagation(); handleRename(ref); }}>重命名</a>{" "}
                <a onClick={(e) => { e.stopPropagation(); handleDelete(ref); }}>删除</a>
              </span>
            </span>
          );
        }}
      />
    </div>
  );
}
