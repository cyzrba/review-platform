<script setup>
import { ArrowRight, Refresh } from '@element-plus/icons-vue'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import { getDashboard, getIstudyStatus, listRubrics } from '@/api'

const router = useRouter()
const loading = ref(true)
const data = ref(null)
const rubrics = ref([])
const istudy = ref(null)
const istudyLoading = ref(false)

const today = new Intl.DateTimeFormat('zh-CN', {
  month: 'long',
  day: 'numeric',
  weekday: 'long',
}).format(new Date())

const progressRate = computed(() => {
  const total = Number(data.value?.result_count || 0)
  const graded = Number(data.value?.graded_count || 0)
  return total ? Math.round((graded / total) * 100) : 0
})

const pendingCopy = computed(() => {
  if (!data.value) return '正在同步评审进度'
  if (!data.value.result_count) return '导入作业后，评审进度会显示在这里'
  if (!data.value.pending_count) return '当前所有评分均已完成'
  return `还有 ${data.value.pending_count} 份作业等待评审`
})

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

/** i学习浏览器状态单独查询，避免拖慢整页加载。 */
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

function refreshAll() {
  load()
  loadIstudy()
}

function progressOf(row) {
  const total = Number(row.stats?.result_count || 0)
  const graded = Number(row.stats?.graded_count || 0)
  return total ? Math.round((graded / total) * 100) : 0
}
</script>

<template>
  <div class="page dashboard-page" v-loading="loading">
    <header class="dashboard-heading">
      <div>
        <div class="eyebrow"><span></span> Overview</div>
        <h1 class="page-title">评审工作台</h1>
      </div>
      <div class="heading-actions">
        <span class="today">{{ today }}</span>
        <el-button :icon="Refresh" circle aria-label="刷新数据" @click="refreshAll" />
      </div>
    </header>

    <template v-if="data">
      <section class="overview-grid">
        <article class="review-hero">
          <div class="review-hero-copy">
            <span class="section-kicker">评审进度</span>
            <h2>{{ pendingCopy }}</h2>
            <div class="hero-actions">
              <el-button type="primary" @click="router.push('/grading')">
                进入评审
                <el-icon class="el-icon--right"><ArrowRight /></el-icon>
              </el-button>
              <button class="text-action" @click="router.push('/classes')">
                管理学生名单
              </button>
            </div>
          </div>
          <div
            class="progress-orbit"
            :style="{ '--progress': `${progressRate * 3.6}deg` }"
            :aria-label="`已完成 ${progressRate}%`"
          >
            <div class="progress-orbit-inner">
              <strong>{{ progressRate }}</strong>
              <span>% 已完成</span>
            </div>
          </div>
          <span class="hero-cross" aria-hidden="true"></span>
        </article>

        <aside class="connection-panel surface">
          <div class="panel-heading compact">
            <div>
              <span class="section-kicker">运行状态</span>
              <h3>连接与存储</h3>
            </div>
          </div>

          <div class="connection-row" v-loading="istudyLoading">
            <span class="connection-icon">IS</span>
            <div class="connection-copy">
              <strong>i学习</strong>
              <span>{{ istudy?.available ? '浏览器已连接' : '等待浏览器连接' }}</span>
            </div>
            <span
              class="status-pill"
              :class="istudy?.available ? 'is-online' : 'is-offline'"
            >
              {{ istudy?.available ? (istudy.logged_in ? '已登录' : '未登录') : '未连接' }}
            </span>
          </div>

          <div class="connection-row">
            <span class="connection-icon storage">ST</span>
            <div class="connection-copy">
              <strong>文件存储</strong>
              <span>{{ data.storage.endpoint || '本地磁盘' }}</span>
            </div>
            <span class="status-pill" :class="data.storage.ok ? 'is-online' : 'is-offline'">
              {{ data.storage.ok ? '正常' : '异常' }}
            </span>
          </div>

          <button class="panel-link" @click="loadIstudy">
            重新检测
            <el-icon><ArrowRight /></el-icon>
          </button>
        </aside>
      </section>

      <section class="metric-strip" aria-label="平台数据概览">
        <div class="metric-item">
          <span>课程</span>
          <strong>{{ data.course_count }}</strong>
          <small>COURSES</small>
        </div>
        <div class="metric-item">
          <span>班级</span>
          <strong>{{ data.class_count }}</strong>
          <small>CLASSES</small>
        </div>
        <div class="metric-item">
          <span>学生</span>
          <strong>{{ data.student_count }}</strong>
          <small>STUDENTS</small>
        </div>
        <div class="metric-item">
          <span>评分细则</span>
          <strong>{{ data.rubric_count }}</strong>
          <small>RUBRICS</small>
        </div>
        <div class="metric-item">
          <span>评分结果</span>
          <strong>{{ data.result_count }}</strong>
          <small>RESULTS</small>
        </div>
        <div class="metric-item metric-positive">
          <span>已评审</span>
          <strong>{{ data.graded_count }}</strong>
          <small>REVIEWED</small>
        </div>
        <div class="metric-item metric-pending">
          <span>待评审</span>
          <strong>{{ data.pending_count }}</strong>
          <small>PENDING</small>
        </div>
      </section>

      <section class="dashboard-content">
        <article class="surface rubric-panel">
          <div class="panel-heading">
            <div>
              <span class="section-kicker">Recent</span>
              <h3>评分细则</h3>
            </div>
            <el-button class="plain-button" @click="router.push('/rubrics')">
              查看全部
              <el-icon class="el-icon--right"><ArrowRight /></el-icon>
            </el-button>
          </div>

          <el-table :data="rubrics" empty-text="还没有评分细则" class="clean-table">
            <el-table-column prop="name" label="项目 / 作业" min-width="170">
              <template #default="{ row }">
                <button class="table-title" @click="router.push(`/grading/${row.id}`)">
                  {{ row.name }}
                </button>
              </template>
            </el-table-column>
            <el-table-column label="类型" width="88">
              <template #default="{ row }">
                <el-tag size="small" :type="row.kind === 'lab_report' ? 'warning' : 'info'">
                  {{ row.kind === 'lab_report' ? '实验报告' : '作业' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="课程" min-width="110">
              <template #default="{ row }">
                <span class="course-name">{{ row.courses?.[0]?.name || '未挂课程' }}</span>
              </template>
            </el-table-column>
            <el-table-column label="进度" width="125">
              <template #default="{ row }">
                <div class="table-progress">
                  <span class="progress-track">
                    <i :style="{ width: `${progressOf(row)}%` }"></i>
                  </span>
                  <span>{{ progressOf(row) }}%</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column label="平均分" width="60" align="right">
              <template #default="{ row }">
                <strong class="score-number">{{ row.stats.average_score ?? '—' }}</strong>
              </template>
            </el-table-column>
            <el-table-column width="42" fixed="right">
              <template #default="{ row }">
                <button
                  class="round-link"
                  aria-label="进入评审"
                  @click="router.push(`/grading/${row.id}`)"
                >
                  <el-icon><ArrowRight /></el-icon>
                </button>
              </template>
            </el-table-column>
          </el-table>
        </article>

        <aside class="surface quick-panel">
          <div class="panel-heading">
            <div>
              <span class="section-kicker">Shortcuts</span>
              <h3>快捷入口</h3>
            </div>
          </div>
          <button class="quick-link" @click="router.push('/classes')">
            <span class="quick-index">01</span>
            <span>
              <strong>整理班级名单</strong>
              <small>课程、班级与学生</small>
            </span>
            <el-icon><ArrowRight /></el-icon>
          </button>
          <button class="quick-link" @click="router.push('/rubrics')">
            <span class="quick-index">02</span>
            <span>
              <strong>设置评分细则</strong>
              <small>评分标准与满分</small>
            </span>
            <el-icon><ArrowRight /></el-icon>
          </button>
          <button class="quick-link" @click="router.push('/grading')">
            <span class="quick-index">03</span>
            <span>
              <strong>开始评审</strong>
              <small>AI 评阅与成绩导出</small>
            </span>
            <el-icon><ArrowRight /></el-icon>
          </button>
        </aside>
      </section>
    </template>
  </div>
</template>
