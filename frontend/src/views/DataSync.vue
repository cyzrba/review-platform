<script setup>
import { Download, Refresh, View } from '@element-plus/icons-vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import {
  compareIstudyLabStudents,
  compareIstudyStudents,
  exportIstudyLabReport,
  exportIstudyWork,
  fileDownloadUrl,
  getIstudyStatus,
  listCourseClasses,
  listCourses,
  listIstudyLabReports,
  listIstudyLabSubmissions,
  listIstudySubmissions,
  listIstudyWorks,
  syncIstudyLabReports,
  syncIstudyWorks,
  syncIstudyWorkScores,
} from '@/api'

const emptyList = () => ({ items: [], total: 0, last_synced_at: null })

const courses = ref([])
const courseId = ref(null)
const courseClasses = ref([])
const classId = ref('all')
const source = ref('work')
const workData = ref(emptyList())
const labData = ref(emptyList())
const loading = ref(false)
const syncing = ref(false)
const statusLoading = ref(false)
const istudyStatus = ref(null)
const monitoring = ref(false)
const busyGroups = ref(new Set())

const detailDrawer = ref(false)
const detailGroup = ref(null)
const detailRows = ref([])
const detailLoading = ref(false)

const gradeDrawer = ref(false)
const gradeGroup = ref(null)
const gradeRows = ref([])
const gradeLoading = ref(false)

let monitorTimer = null
let monitorTicks = 0
let monitorLoading = false

const activeData = computed(() => (source.value === 'lab' ? labData.value : workData.value))
const groups = computed(() => activeData.value.items || [])

const summary = computed(() => {
  const items = groups.value
  const scoreCount = items.reduce((total, item) => total + Number(item.score_count || 0), 0)
  const scoreTotal = items.reduce(
    (total, item) =>
      total + Number(item.average_istudy_score || 0) * Number(item.score_count || 0),
    0,
  )
  return {
    tasks: items.length,
    submitted: items.reduce((total, item) => total + Number(item.submitted_count || 0), 0),
    captured: items.reduce((total, item) => total + Number(item.captured_count || 0), 0),
    scores: scoreCount,
    average: scoreCount ? (scoreTotal / scoreCount).toFixed(1) : '—',
  }
})

const lastSyncedAt = computed(() => activeData.value.last_synced_at)

const detailSummary = computed(() => ({
  total: detailRows.value.length,
  scored: detailRows.value.filter((item) => item.istudy_score != null).length,
  downloaded: detailRows.value.filter((item) => item.file_state === 'ready').length,
}))

const gradeSummary = computed(() => {
  const scored = gradeRows.value.filter((item) => item.ai_score != null)
  return {
    total: gradeRows.value.length,
    scored: scored.length,
    average: scored.length
      ? (
          scored.reduce((total, item) => total + Number(item.ai_score), 0) / scored.length
        ).toFixed(1)
      : '—',
  }
})

const workStatusMeta = {
  not_started: { label: '未开始', type: 'info' },
  ongoing: { label: '进行中', type: 'warning' },
  ended: { label: '已结束', type: 'success' },
}

const fileStateMeta = {
  none: { label: '未下载', type: 'info' },
  pending: { label: '下载中', type: 'warning' },
  ready: { label: '已入库', type: 'success' },
  failed: { label: '失败', type: 'danger' },
}

function formatDateTime(value) {
  if (!value) return '尚未同步'
  return String(value).replace('T', ' ').slice(0, 16)
}

function formatScore(value) {
  const number = Number(value)
  return Number.isInteger(number) ? String(number) : number.toFixed(1)
}

function emptyImageText(row) {
  if (row.state !== 'submitted') return '无图片'
  return row.file_state === 'ready' ? '无图片' : '尚未下载'
}

function groupKey(row) {
  return `${row.source}:${row.library_id || row.name}`
}

function statusMeta(row) {
  return workStatusMeta[row.status] || { label: '多个状态', type: 'info' }
}

function pendingDownloadCount(row) {
  return (row.classes || []).reduce(
    (total, item) =>
      total +
      Math.max(
        0,
        Number(item.submitted_count || 0) -
          (item.sync_state === 'running' ? Number(item.submitted_count || 0) : item.captured_count || 0),
      ),
    0,
  )
}

function attachmentPercent(row) {
  const submitted = Number(row.submitted_count || 0)
  if (!submitted) return 0
  return Math.min(100, Math.round((Number(row.captured_count || 0) / submitted) * 100))
}

async function loadStatus() {
  statusLoading.value = true
  try {
    istudyStatus.value = await getIstudyStatus()
  } catch (error) {
    istudyStatus.value = { available: false, logged_in: false, message: error.message }
  } finally {
    statusLoading.value = false
  }
}

async function loadLists({ quiet = false } = {}) {
  if (!courseId.value) return
  if (!quiet) loading.value = true
  try {
    const [workResult, labResult] = await Promise.allSettled([
      listIstudyWorks({
        course_id: courseId.value,
        class_id: classId.value === 'all' ? undefined : classId.value,
      }),
      listIstudyLabReports({
        course_id: courseId.value,
        class_id: classId.value === 'all' ? undefined : classId.value,
      }),
    ])
    if (workResult.status === 'fulfilled') {
      workData.value = workResult.value
    } else if (!quiet) {
      ElMessage.error(`作业列表加载失败：${workResult.reason.message}`)
    }
    if (labResult.status === 'fulfilled') {
      labData.value = labResult.value
    } else if (!quiet) {
      ElMessage.error(`实验报告列表加载失败：${labResult.reason.message}`)
    }
  } finally {
    if (!quiet) loading.value = false
  }
}

async function loadCourseClasses() {
  if (!courseId.value) {
    courseClasses.value = []
    return
  }
  try {
    const data = await listCourseClasses(courseId.value, { page: 1, page_size: 200 })
    courseClasses.value = data.items
  } catch (error) {
    courseClasses.value = []
    throw error
  }
}

async function loadCourses() {
  try {
    const data = await listCourses({ page: 1, page_size: 200 })
    courses.value = data.items
    if (!courses.value.some((item) => item.id === courseId.value)) {
      courseId.value = courses.value[0]?.id ?? null
      classId.value = 'all'
    }
    await loadCourseClasses()
    await loadLists()
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function changeCourse() {
  classId.value = 'all'
  try {
    await loadCourseClasses()
  } catch (error) {
    ElMessage.error(`班级列表加载失败：${error.message}`)
  }
  await loadLists()
}

function messageFor(result, mode) {
  return result.status === 'fulfilled' ? result.value.message : `${mode}：${result.reason.message}`
}

async function syncAll() {
  if (!courseId.value) {
    ElMessage.warning('请先选择课程')
    return
  }
  syncing.value = true
  try {
    const listResults = await Promise.allSettled([
      syncIstudyWorks({ course_id: courseId.value }),
      syncIstudyLabReports({ course_id: courseId.value }),
    ])
    const scoreResult = await Promise.allSettled([
      syncIstudyWorkScores(courseId.value),
    ])

    const messages = [
      messageFor(listResults[0], '作业同步失败'),
      messageFor(listResults[1], '实验报告同步失败'),
      messageFor(scoreResult[0], '作业成绩同步失败'),
    ]
    const succeeded = listResults.filter((item) => item.status === 'fulfilled').length
    if (succeeded) {
      ElMessage.success(messages.join('；'))
    } else {
      ElMessage.warning(messages.join('；'))
    }
    await loadLists()
  } finally {
    syncing.value = false
  }
}

function markBusy(key, busy) {
  const next = new Set(busyGroups.value)
  if (busy) next.add(key)
  else next.delete(key)
  busyGroups.value = next
}

function isBusy(row) {
  return busyGroups.value.has(groupKey(row))
}

function hasActiveBackgroundJobs() {
  const all = [...workData.value.items, ...labData.value.items]
  return all.some((group) =>
    (group.classes || []).some((item) => {
      const exportStatus = item.last_export?.status
      return (
        item.sync_state === 'running' ||
        ['queued', 'exporting', 'downloading', 'parsing'].includes(exportStatus)
      )
    }),
  )
}

function stopMonitor() {
  if (monitorTimer) {
    clearInterval(monitorTimer)
    monitorTimer = null
  }
  monitoring.value = false
  monitorTicks = 0
}

function startMonitor() {
  if (monitorTimer) return
  monitoring.value = true
  monitorTicks = 0
  monitorTimer = setInterval(async () => {
    if (monitorLoading) return
    monitorLoading = true
    monitorTicks += 1
    try {
      await loadLists({ quiet: true })
      if (!hasActiveBackgroundJobs() || monitorTicks >= 180) stopMonitor()
    } finally {
      monitorLoading = false
    }
  }, 5000)
}

async function downloadGroup(row) {
  const key = groupKey(row)
  if (isBusy(row)) return
  const pending = pendingDownloadCount(row)
  if (!pending) {
    ElMessage.info('这次任务没有新的提交附件需要下载')
    return
  }

  markBusy(key, true)
  let triggered = 0
  const failures = []
  try {
    for (const item of row.classes || []) {
      if (Number(item.captured_count || 0) >= Number(item.submitted_count || 0)) continue
      try {
        if (row.source === 'lab') {
          const result = await exportIstudyLabReport(item.work_id)
          triggered += Number(result.queued || 0)
          continue
        }

        const compare = await compareIstudyStudents(item.work_id)
        const userIds = (compare.students || [])
          .filter((student) => student.state === 'new' && student.istudy_user_id)
          .map((student) => student.istudy_user_id)
        if (!userIds.length) continue
        await exportIstudyWork(item.work_id, { person_ids: userIds })
        triggered += userIds.length
      } catch (error) {
        failures.push(`${item.class_name || item.work_id}：${error.message}`)
      }
    }
    if (triggered) {
      startMonitor()
      ElMessage.success(`已提交 ${triggered} 份提交的下载任务，页面会自动刷新进度`)
    } else if (failures.length) {
      ElMessage.error(failures.slice(0, 2).join('；'))
    } else {
      ElMessage.info('i学习 上没有发现新的提交附件')
    }
  } finally {
    markBusy(key, false)
  }
}

async function fetchGroupSubmissions(row) {
  const requests = (row.classes || []).map((item) =>
    row.source === 'lab'
      ? listIstudyLabSubmissions(item.work_id)
      : listIstudySubmissions(item.work_id),
  )
  if (!requests.length) return []
  const result = await Promise.allSettled(requests)
  const fulfilled = result.filter((item) => item.status === 'fulfilled')
  if (!fulfilled.length) {
    throw new Error(result[0]?.reason?.message || '学生数据加载失败')
  }
  if (fulfilled.length < result.length) {
    ElMessage.warning('部分班级的学生数据加载失败，当前展示已加载的班级')
  }
  return fulfilled
    .flatMap((item) => item.value)
    .sort((a, b) =>
      `${a.class_name || ''}${a.student_no || ''}`.localeCompare(
        `${b.class_name || ''}${b.student_no || ''}`,
        'zh-CN',
      ),
    )
}

async function loadDetails(row) {
  detailLoading.value = true
  try {
    detailRows.value = await fetchGroupSubmissions(row)
  } catch (error) {
    detailRows.value = []
    ElMessage.error(`详情加载失败：${error.message}`)
  } finally {
    detailLoading.value = false
  }
}

async function openDetails(row) {
  detailGroup.value = row
  detailRows.value = []
  detailDrawer.value = true
  await loadDetails(row)
}

async function loadGrades(row) {
  gradeLoading.value = true
  try {
    gradeRows.value = await fetchGroupSubmissions(row)
  } catch (error) {
    gradeRows.value = []
    ElMessage.error(`成绩加载失败：${error.message}`)
  } finally {
    gradeLoading.value = false
  }
}

async function openGrades(row) {
  gradeGroup.value = row
  gradeRows.value = []
  gradeDrawer.value = true
  await loadGrades(row)
}

onMounted(async () => {
  await Promise.all([loadStatus(), loadCourses()])
})

onBeforeUnmount(stopMonitor)
</script>

<template>
  <div class="page data-sync-page">
    <div class="page-header">
      <div>
        <h2 class="page-title">数据同步</h2>
      </div>
      <div class="page-actions">
        <el-button :icon="Refresh" :loading="loading" @click="loadLists">刷新</el-button>
        <el-button
          type="primary"
          :loading="syncing"
          :disabled="!courseId || !istudyStatus?.logged_in"
          @click="syncAll"
        >
          同步列表与成绩
        </el-button>
      </div>
    </div>

    <section class="sync-controls surface">
      <div class="sync-course">
        <span>课程</span>
        <el-select
          v-model="courseId"
          placeholder="选择课程"
          filterable
          style="width: 280px"
          @change="changeCourse"
        >
          <el-option v-for="item in courses" :key="item.id" :label="item.name" :value="item.id" />
        </el-select>
      </div>

      <div class="sync-course">
        <span>班级</span>
        <el-select
          v-model="classId"
          placeholder="全部班级"
          filterable
          style="width: 220px"
          @change="loadLists()"
        >
          <el-option label="全部班级" value="all" />
          <el-option
            v-for="item in courseClasses"
            :key="item.id"
            :label="item.full_name || item.name"
            :value="item.id"
          />
        </el-select>
      </div>

      <div class="sync-source-tabs" role="tablist" aria-label="数据类型">
        <button
          :class="{ active: source === 'work' }"
          role="tab"
          :aria-selected="source === 'work'"
          @click="source = 'work'"
        >
          作业
          <span>{{ workData.total }}</span>
        </button>
        <button
          :class="{ active: source === 'lab' }"
          role="tab"
          :aria-selected="source === 'lab'"
          @click="source = 'lab'"
        >
          实验报告
          <span>{{ labData.total }}</span>
        </button>
      </div>

      <div class="sync-connection" v-loading="statusLoading">
        <i :class="{ online: istudyStatus?.available }"></i>
        <span>{{ istudyStatus?.available ? 'i学习 已连接' : 'i学习 未连接' }}</span>
        <button @click="loadStatus">重新检测</button>
      </div>
    </section>

    <section class="sync-metrics" aria-label="同步数据概览">
      <div>
        <span>任务</span>
        <strong>{{ summary.tasks }}</strong>
        <small>已同步到本地</small>
      </div>
      <div>
        <span>已交</span>
        <strong>{{ summary.submitted }}</strong>
        <small>i学习 提交记录</small>
      </div>
      <div>
        <span>附件已入库</span>
        <strong>{{ summary.captured }}</strong>
        <small>再次使用无需下载</small>
      </div>
      <div>
        <span>已有成绩</span>
        <strong>{{ summary.scores }}</strong>
        <small>平均 {{ summary.average }} 分</small>
      </div>
    </section>

    <section class="surface sync-list-panel" v-loading="loading">
      <div class="panel-heading">
        <div>
          <span class="section-kicker">{{ source === 'lab' ? 'Lab reports' : 'Homework' }}</span>
          <h3>{{ source === 'lab' ? '实验报告数据' : '作业数据' }}</h3>
        </div>
        <div class="sync-list-meta">
          <span v-if="monitoring" class="monitor-status"><i></i> 正在下载并入库</span>
          <span>最近同步 {{ formatDateTime(lastSyncedAt) }}</span>
        </div>
      </div>

      <el-table :data="groups" empty-text="还没有同步数据">
        <el-table-column prop="name" :label="source === 'lab' ? '实验报告' : '作业'" min-width="170">
          <template #default="{ row }">
            <div class="sync-name-cell">
              <strong>{{ row.name }}</strong>
              <span>{{ row.class_count }} 个班级</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="84">
          <template #default="{ row }">
            <el-tag size="small" :type="statusMeta(row).type">{{ statusMeta(row).label }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="提交" width="65" align="center">
          <template #default="{ row }">{{ row.submitted_count }}</template>
        </el-table-column>
        <el-table-column label="附件入库" min-width="120">
          <template #default="{ row }">
            <div class="sync-progress-cell">
              <span>
                {{ row.captured_count }} / {{ row.submitted_count }}
              </span>
              <span class="progress-track">
                <i :style="{ width: `${attachmentPercent(row)}%` }"></i>
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="已有成绩" width="110">
          <template #default="{ row }">
            <div class="sync-name-cell">
              <strong>{{ row.score_count }} 人</strong>
              <span>平均 {{ row.average_istudy_score ?? '—' }} 分</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="270" fixed="right">
          <template #default="{ row }">
            <div class="sync-actions">
              <el-button
                link
                type="primary"
                :icon="Download"
                :loading="isBusy(row)"
                :disabled="!pendingDownloadCount(row)"
                @click="downloadGroup(row)"
              >
                {{ pendingDownloadCount(row) ? `下载 ${pendingDownloadCount(row)} 份` : '已下载' }}
              </el-button>
              <el-button link :icon="View" @click="openDetails(row)">查看详情</el-button>
              <el-button link @click="openGrades(row)">查看成绩</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <el-drawer v-model="detailDrawer" :title="`提交详情 · ${detailGroup?.name || ''}`" size="880px">
      <div class="score-overview">
        <div><span>学生记录</span><strong>{{ detailSummary.total }}</strong></div>
        <div><span>i学习已有成绩</span><strong>{{ detailSummary.scored }}</strong></div>
        <div><span>附件已入库</span><strong>{{ detailSummary.downloaded }}</strong></div>
      </div>
      <el-table
        :data="detailRows"
        v-loading="detailLoading"
        max-height="650"
        empty-text="还没有学生提交数据"
      >
        <el-table-column prop="class_name" label="班级" width="130" />
        <el-table-column prop="student_no" label="学号" width="135" />
        <el-table-column prop="student_name" label="姓名" width="100" />
        <el-table-column label="提交状态" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.state === 'submitted' ? 'success' : 'info'">
              {{ row.state === 'submitted' ? '已交' : '未交' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="附件" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="fileStateMeta[row.file_state]?.type">
              {{ fileStateMeta[row.file_state]?.label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="i学习 分数" width="110" align="right">
          <template #default="{ row }">
            <strong class="score-number">{{ row.istudy_score ?? '—' }}</strong>
          </template>
        </el-table-column>
        <el-table-column label="提交时间" min-width="150">
          <template #default="{ row }">{{ formatDateTime(row.submitted_at) }}</template>
        </el-table-column>
      </el-table>
    </el-drawer>

    <el-drawer v-model="gradeDrawer" :title="`AI 评阅成绩 · ${gradeGroup?.name || ''}`" size="980px">
      <div class="grade-toolbar">
        <span>成绩与“评审与成绩”页实时关联，未评阅的学生显示“无”。</span>
        <el-button size="small" :loading="gradeLoading" @click="loadGrades(gradeGroup)">刷新</el-button>
      </div>
      <div class="score-overview">
        <div><span>学生人数</span><strong>{{ gradeSummary.total }}</strong></div>
        <div><span>已有 AI 评分</span><strong>{{ gradeSummary.scored }}</strong></div>
        <div><span>平均分</span><strong>{{ gradeSummary.average }}</strong></div>
      </div>
      <el-table
        :data="gradeRows"
        v-loading="gradeLoading"
        max-height="650"
        empty-text="还没有学生提交数据"
      >
        <el-table-column prop="class_name" label="班级" width="130" />
        <el-table-column label="姓名" width="120">
          <template #default="{ row }">
            <div class="sync-name-cell">
              <strong>{{ row.student_name || '—' }}</strong>
              <span>{{ row.student_no }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="作业图片" min-width="420">
          <template #default="{ row }">
            <div v-if="row.image_keys?.length" class="grade-image-list">
              <el-image
                v-for="(key, index) in row.image_keys"
                :key="key"
                :src="fileDownloadUrl(key)"
                :preview-src-list="row.image_keys.map((item) => fileDownloadUrl(item))"
                :initial-index="index"
                fit="cover"
                lazy
                preview-teleported
                class="grade-image"
              />
            </div>
            <span v-else class="grade-empty">{{ emptyImageText(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="AI 自动评阅成绩" width="170" align="center" header-align="center">
          <template #default="{ row }">
            <div class="grade-score-cell">
              <template v-if="row.ai_score != null">
                <strong>{{ formatScore(row.ai_score) }}</strong>
                <span>分</span>
                <small v-if="row.ai_is_manual">人工调整</small>
              </template>
              <span v-else class="grade-empty">无</span>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-drawer>
  </div>
</template>
