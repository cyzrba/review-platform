<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { getDashboard, getIstudyStatus, listRubrics } from '@/api'

const router = useRouter()
const loading = ref(true)
const data = ref(null)
const rubrics = ref([])
const istudy = ref(null)
const istudyLoading = ref(false)

async function load() {
  loading.value = true
  try {
    const [dashboard, rubricList] = await Promise.all([
      getDashboard(),
      listRubrics({ page: 1, page_size: 8 }),
    ])
    data.value = dashboard
    rubrics.value = rubricList.items
  } catch (error) {
    ElMessage.error(`加载失败：${error.message}`)
  } finally {
    loading.value = false
  }
}

onMounted(load)

/** i学习 浏览器状态单独查：它要连本机 Edge 的调试端口，比别的接口慢，不拖着整页加载。 */
async function loadIstudy() {
  istudyLoading.value = true
  try {
    istudy.value = await getIstudyStatus()
  } catch (error) {
    istudy.value = {
      available: false,
      logged_in: false,
      message: `查询失败：${error.message}`,
    }
  } finally {
    istudyLoading.value = false
  }
}

onMounted(loadIstudy)

/** 顶部「刷新」把整页都刷一遍，包含 i学习 连接状态。 */
function refreshAll() {
  load()
  loadIstudy()
}
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">概览</h2>
        <div class="page-subtitle">
          从 i学习 抓名单与作业 → AI 评审 → 导出成绩与评语
        </div>
      </div>
      <el-button @click="refreshAll">刷新</el-button>
    </div>

    <div class="stat-grid" v-if="data">
      <div class="stat-card">
        <div class="label">课程</div>
        <div class="value">{{ data.course_count }}</div>
      </div>
      <div class="stat-card">
        <div class="label">班级</div>
        <div class="value">{{ data.class_count }}</div>
      </div>
      <div class="stat-card">
        <div class="label">学生</div>
        <div class="value">{{ data.student_count }}</div>
      </div>
      <div class="stat-card">
        <div class="label">评分细则 / 项目</div>
        <div class="value">{{ data.rubric_count }}</div>
      </div>
      <div class="stat-card">
        <div class="label">评分结果</div>
        <div class="value">{{ data.result_count }}</div>
      </div>
      <div class="stat-card">
        <div class="label">已评审</div>
        <div class="value" style="color: #2f9e44">{{ data.graded_count }}</div>
      </div>
      <div class="stat-card">
        <div class="label">待评审</div>
        <div class="value" style="color: #e8590c">{{ data.pending_count }}</div>
      </div>
    </div>

    <div class="table-card" v-if="data" style="margin-bottom: 16px">
      <el-descriptions :column="3" border>
        <el-descriptions-item label="文件存储">
          {{ data.storage.backend }}
          <el-tag :type="data.storage.ok ? 'success' : 'danger'" size="small" style="margin-left: 8px">
            {{ data.storage.ok ? '正常' : '不可用' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="服务地址">
          {{ data.storage.endpoint || '本地磁盘' }}
        </el-descriptions-item>
        <el-descriptions-item label="Bucket">{{ data.storage.bucket }}</el-descriptions-item>
      </el-descriptions>
    </div>

    <div class="table-card" style="margin-bottom: 16px" v-loading="istudyLoading">
      <div class="toolbar" style="margin-bottom: 0">
        <span style="font-weight: 600">i学习 浏览器</span>
        <template v-if="istudy">
          <el-tag :type="istudy.available ? 'success' : 'danger'" size="small">
            {{ istudy.available ? '已连接' : '未连接' }}
          </el-tag>
          <el-tag
            v-if="istudy.available"
            :type="istudy.logged_in ? 'success' : 'warning'"
            size="small"
          >
            {{ istudy.logged_in ? '已登录' : '未登录' }}
          </el-tag>
        </template>
        <el-button link type="primary" @click="loadIstudy">重新检测</el-button>
      </div>
    </div>

    <div class="table-card">
      <div style="font-weight: 600; margin-bottom: 12px">评分细则</div>
      <el-table :data="rubrics" empty-text="还没有评分细则">
        <el-table-column prop="name" label="项目 / 作业" min-width="220">
          <template #default="{ row }">
            <el-link type="primary" @click="router.push(`/grading/${row.id}`)">{{ row.name }}</el-link>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.kind === 'lab_report' ? 'warning' : ''">
              {{ row.kind === 'lab_report' ? '实验报告' : '作业' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="所属课程" min-width="160">
          <template #default="{ row }">
            <el-tag v-for="course in row.courses" :key="course.id" size="small" style="margin-right: 6px">
              {{ course.name }}
            </el-tag>
            <span v-if="!row.courses?.length" class="muted">未挂课程</span>
          </template>
        </el-table-column>
        <el-table-column label="满分" width="80">
          <template #default="{ row }">{{ row.total_score }}</template>
        </el-table-column>
        <el-table-column label="进度" width="240">
          <template #default="{ row }">
            <span class="muted">
              {{ row.stats.class_count }} 个班级 · {{ row.stats.result_count }} 条结果 ·
              已评审 {{ row.stats.graded_count }} · 待评审 {{ row.stats.pending_count }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="平均分" width="100">
          <template #default="{ row }">{{ row.stats.average_score ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="110" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="router.push(`/grading/${row.id}`)">进入评审</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>
