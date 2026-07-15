import { BookOutlined, ExpandOutlined, LoadingOutlined, ToolOutlined } from "@ant-design/icons";
import { Badge, Empty, List, Skeleton, Space, Spin, Tag, Typography } from "antd";
import { useCallback, useEffect, useState } from "react";

import { listAssistantTools, listSkills } from "../../api";
import type { AssistantTool, Skill } from "../../types";

import { SkillDrawer } from "./SkillDrawer";

const { Text } = Typography;

const CATEGORY_COLORS: Record<string, string> = {
  agent: "blue",
  backtest: "green",
  strategy: "purple",
  data: "orange",
  analysis: "cyan",
  crypto: "gold",
  flow: "magenta",
  other: "default",
};

function categoryColor(category: string): string {
  return CATEGORY_COLORS[category.toLowerCase()] ?? "default";
}

export function SkillsToolsTab() {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [tools, setTools] = useState<AssistantTool[]>([]);
  const [loading, setLoading] = useState(true);
  const [drawerSkill, setDrawerSkill] = useState<Skill | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  useEffect(() => {
    let alive = true;
    void (async () => {
      setLoading(true);
      try {
        const [skillRows, toolRows] = await Promise.all([listSkills(), listAssistantTools()]);
        if (!alive) return;
        setSkills(skillRows);
        setTools(toolRows);
      } catch (err) {
        if (alive) console.error("Failed to load skills/tools:", err);
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  const openDrawer = useCallback((skill: Skill) => {
    setDrawerSkill(skill);
    setDrawerOpen(true);
  }, []);

  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
    setDrawerSkill(null);
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center p-8">
        <Spin indicator={<LoadingOutlined spin />} tip="加载中..." />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 overflow-auto">
      {/* Skills section */}
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <BookOutlined />
          <Typography.Text strong>Skills</Typography.Text>
          <Badge count={skills.length} style={{ backgroundColor: "#1677ff" }} />
        </div>
        {skills.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可用 Skills" />
        ) : (
          <List
            size="small"
            dataSource={skills}
            rowKey="folder_name"
            renderItem={(skill) => (
              <List.Item
                className="!items-start !py-2 cursor-pointer hover:bg-black/5"
                onClick={() => openDrawer(skill)}
              >
                <Space direction="vertical" size={2} className="w-full">
                  <Typography.Text strong>{skill.frontmatter.name}</Typography.Text>
                  <Text type="secondary" className="text-xs">
                    {skill.frontmatter.description}
                  </Text>
                </Space>
                <ExpandOutlined className="text-gray-400" />
              </List.Item>
            )}
          />
        )}
      </div>

      <div className="border-t border-shell-line" />

      {/* Tools section */}
      <div className="flex flex-col gap-2">
        <div className="flex items-center gap-2">
          <ToolOutlined />
          <Typography.Text strong>Tools</Typography.Text>
          <Badge count={tools.length} style={{ backgroundColor: "#1677ff" }} />
        </div>
        {tools.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无可用 Tools" />
        ) : (
          <List
            size="small"
            dataSource={tools}
            rowKey="name"
            renderItem={(tool) => (
              <List.Item className="!items-start !py-2">
                <Space direction="vertical" size={2} className="w-full">
                  <Space wrap>
                    <Typography.Text code>{tool.name}</Typography.Text>
                    <Tag color={categoryColor(tool.category)}>{tool.category}</Tag>
                  </Space>
                  <Text type="secondary" className="text-xs">
                    {tool.description}
                  </Text>
                </Space>
              </List.Item>
            )}
          />
        )}
      </div>

      <SkillDrawer skill={drawerSkill} open={drawerOpen} onClose={closeDrawer} />
    </div>
  );
}
