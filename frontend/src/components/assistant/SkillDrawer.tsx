import { Drawer, Typography } from "antd";

import type { Skill } from "../../types";

const { Text } = Typography;

interface SkillDrawerProps {
  skill: Skill | null;
  open: boolean;
  onClose: () => void;
}

export function SkillDrawer({ skill, open, onClose }: SkillDrawerProps) {
  return (
    <Drawer
      title={
        <div className="flex items-center gap-2">
          <span>{skill?.frontmatter.name ?? ""}</span>
        </div>
      }
      placement="right"
      width={520}
      open={open}
      onClose={onClose}
      destroyOnClose
    >
      <div className="flex flex-col gap-4">
        {skill?.frontmatter.description ? (
          <Text type="secondary">{skill.frontmatter.description}</Text>
        ) : null}
        {skill?.frontmatter.license ? (
          <div className="border-t border-shell-line pt-3">
            <Text type="secondary" className="text-xs">
              License: {skill.frontmatter.license}
            </Text>
          </div>
        ) : null}
      </div>
    </Drawer>
  );
}
