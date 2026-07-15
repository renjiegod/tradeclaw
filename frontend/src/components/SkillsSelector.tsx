import { Card, Checkbox, Typography } from "antd";
import { useEffect, useState } from "react";

import { listSkills } from "../api";
import type { Skill } from "../types";

type Props = {
  value?: string[]; // 选中的 skill name 列表
  onChange?: (skills: string[]) => void;
  /** 为 true 时只读展示，不可勾选（与实例表单在不可用状态下行为一致） */
  disabled?: boolean;
};

const { Text, Paragraph: TypographyParagraph } = Typography;

export function SkillsSelector({ value, onChange, disabled }: Props) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    listSkills()
      .then(setSkills)
      .catch(() => setSkills([]))
      .finally(() => setLoading(false));
  }, []);

  // Derive selected from value on every render so it stays in sync with prop changes
  const selected = new Set(value ?? []);

  const toggleSkill = (name: string) => {
    if (disabled) return;
    const next = new Set(selected);
    if (next.has(name)) {
      next.delete(name);
    } else {
      next.add(name);
    }
    onChange?.(Array.from(next));
  };

  if (loading) {
    return <Text type="secondary">加载技能中...</Text>;
  }

  if (skills.length === 0) {
    return <Text type="secondary">未找到可用技能（.doyoutrade/skills/ 为空）</Text>;
  }

  return (
    <div
      style={{ display: "flex", flexDirection: "column", gap: 8 }}
      className={disabled ? "pointer-events-none opacity-50" : undefined}
    >
      {skills.map((skill) => (
        <Card
          key={skill.frontmatter.name}
          size="small"
          hoverable={!disabled}
          style={{
            borderColor: selected.has(skill.frontmatter.name) ? "#1677ff" : undefined,
            backgroundColor: selected.has(skill.frontmatter.name) ? "#e6f4ff" : undefined,
            cursor: "pointer",
          }}
        >
          <div style={{ display: "flex", alignItems: "flex-start", gap: 8 }}>
            <Checkbox
              checked={selected.has(skill.frontmatter.name)}
              disabled={disabled}
              onChange={(e) => {
                e.stopPropagation();
                toggleSkill(skill.frontmatter.name);
              }}
            />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
                <Text strong style={{ fontSize: 13 }}>
                  {skill.frontmatter.name}
                </Text>
                {!skill.enabled && (
                  <Text type="danger" style={{ fontSize: 11 }}>
                    已禁用
                  </Text>
                )}
              </div>
              <TypographyParagraph
                type="secondary"
                style={{ marginBottom: 0, fontSize: 12 }}
                ellipsis={{ rows: 2, expandable: false }}
              >
                {skill.frontmatter.description}
              </TypographyParagraph>
            </div>
          </div>
        </Card>
      ))}
    </div>
  );
}
