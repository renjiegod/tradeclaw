import React from "react";
import { listAssistantAgents } from "../api";
import type { Agent } from "../types";
import { AgentCard } from "../components/AgentCard";
import { AgentFormModal } from "../components/AgentFormModal";
import { Button, Row, Col, Spin } from "antd";
import { usePageRefreshToken } from "../pageRefreshContext";

export function AgentsPage() {
  const pageRefreshToken = usePageRefreshToken();
  const [agents, setAgents] = React.useState<Agent[]>([]);
  const [loading, setLoading] = React.useState(true);
  const [editingAgent, setEditingAgent] = React.useState<Agent | undefined>(undefined);
  const [showForm, setShowForm] = React.useState(false);

  const loadAgents = React.useCallback(async () => {
    setLoading(true);
    try {
      const result = await listAssistantAgents({ include_inactive: true });
      setAgents(result.items);
    } catch (err) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, []);

  React.useEffect(() => {
    void loadAgents();
  }, [loadAgents, pageRefreshToken]);

  const handleSaved = (agent: Agent) => {
    setShowForm(false);
    setEditingAgent(undefined);
    void loadAgents();
  };

  const handleEdit = (agent: Agent) => {
    setEditingAgent(agent);
    setShowForm(true);
  };

  const handleCloneSuccess = (_newAgent: Agent) => {
    void loadAgents();
  };

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
        <h2 style={{ margin: 0 }}>Agents</h2>
        <Button type="primary" onClick={() => { setEditingAgent(undefined); setShowForm(true); }}>
          New Agent
        </Button>
      </div>
      {loading ? (
        <Spin />
      ) : (
        <Row gutter={[16, 16]}>
          {agents.map((agent) => (
            <Col key={agent.id} xs={24} sm={12} md={8} lg={6}>
              <AgentCard
                agent={agent}
                onEdit={handleEdit}
                onDeleted={loadAgents}
                onCloneSuccess={handleCloneSuccess}
              />
            </Col>
          ))}
        </Row>
      )}
      {showForm && (
        <AgentFormModal
          agent={editingAgent}
          onSaved={handleSaved}
          onClose={() => { setShowForm(false); setEditingAgent(undefined); }}
        />
      )}
    </div>
  );
}
