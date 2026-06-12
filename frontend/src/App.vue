<template>
  <div class="layout">
    <nav class="sidebar">
      <div class="brand">
        <span class="brand-icon">⚡</span>
        <span class="brand-name">技能工厂</span>
      </div>
      <RouterLink to="/creator" class="nav-item">
        <span>🛠</span> 技能创建
      </RouterLink>
      <RouterLink to="/creator/tools" class="nav-item">
        <span>🧰</span> 工具管理
      </RouterLink>
      <RouterLink to="/skills" class="nav-item">
        <span>📚</span> 技能库
      </RouterLink>
      <RouterLink to="/sandbox" class="nav-item">
        <span>🧪</span> 沙盒测试
      </RouterLink>
      <div class="sidebar-footer">
        <div class="llm-status" :class="llmStatus">
          <span class="dot"></span>
          {{ llmLabel }}
        </div>
      </div>
    </nav>
    <main class="content">
      <RouterView />
    </main>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { fetchLlmHealth } from './composables/useSkills.js'

const llmStatus = ref('unknown')
const llmLabel = ref('正在检查 LLM…')

onMounted(async () => {
  try {
    const data = await fetchLlmHealth()
    if (data.connected) {
      llmStatus.value = 'ok'
      llmLabel.value = `LLM 已连接`
    } else {
      llmStatus.value = 'err'
      llmLabel.value = 'LLM 离线'
    }
  } catch {
    llmStatus.value = 'err'
    llmLabel.value = 'LLM 离线'
  }
})
</script>

<style scoped>
.layout { display: flex; height: 100vh; overflow: hidden; }

.sidebar {
  width: 200px;
  flex-shrink: 0;
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 16px 12px;
  gap: 4px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 8px 16px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 8px;
}
.brand-icon { font-size: 20px; }
.brand-name { font-weight: 600; font-size: 15px; }

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: var(--radius);
  color: var(--text-muted);
  transition: background 0.15s, color 0.15s;
  font-size: 14px;
}
.nav-item:hover { background: var(--surface2); color: var(--text); }
.nav-item.router-link-active { background: var(--surface2); color: var(--accent); }

.sidebar-footer { margin-top: auto; }

.llm-status {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--text-muted);
  border-radius: var(--radius);
  background: var(--surface2);
}
.dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
  flex-shrink: 0;
}
.llm-status.ok .dot { background: var(--success); }
.llm-status.err .dot { background: var(--danger); }

.content {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
</style>
