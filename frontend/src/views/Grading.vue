<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  PAGE_SIZE,
  applyIstudyBackfill,
  classSummary,
  deleteResult,
  exportCsvUrl,
  exportXlsxUrl,
  fileDownloadUrl,
  gradeIstudyLabReport,
  gradeIstudyWork,
  getResultImages,
  getRubric,
  listCourseClasses,
  listCourses,
  listIstudyLabReports,
  listIstudyWorks,
  listResults,
  listRubrics,
  previewIstudyBackfill,
  restoreRubricResults,
  runGrading,
  runSingleGrading,
  updateResult,
} from '@/api'

const route = useRoute()
const router = useRouter()

const emptyPage = () => ({ items: [], total: 0, page: 1, page_size: PAGE_SIZE, pages: 0 })

const loading = ref(false)
const courses = ref([])
const courseId = ref(null)
const kind = ref('homework')
const rubrics = ref([])
const rubricId = ref(null)
const rubric = ref(null)
// 下面「评分结果」自己的作业选择，跟顶部那个分开
const resultsRubricId = ref(null)
const resultsRubric = ref(null)
const results = ref(emptyPage())
const summaries = ref(emptyPage())
const courseClasses = ref([])
// 成绩表的班级筛选（'all' = 全部班级）
const resultsClassId = ref('all')
const polling = ref(false)
const restoringResults = ref(false)

const drawerVisible = ref(false)
const current = ref(null)
const currentImages = ref([])
const imagesLoading = ref(false)
const editForm = reactive({ score: null, comment: '' })
const savingResult = ref(false)

const statusMeta = {
  pending: { label: '待评审', type: 'info' },
  grading: { label: '评审中', type: 'warning' },
  graded: { label: '已评审', type: 'success' },
  failed: { label: '评审失败', type: 'danger' },
}

// 「一键 AI 评审」弹窗：评分细则在这里选，评分细则只是丢给 AI 看的一份标准
const reviewDialog = ref(false)
const reviewRubricId = ref(null)
const reviewClassId = ref('all')
const reviewForce = ref(false)
const reviewing = ref(false)
const reviewWorks = ref([])
const reviewLoading = ref(false)

const reviewGroup = computed(
  () => reviewWorks.value.find((item) => (item.rubric_ids || []).includes(reviewRubricId.value)) || null,
)

const reviewRubric = computed(
  () => rubrics.value.find((item) => item.id === reviewRubricId.value) || null,
)
const reviewMissingMaterials = computed(() => {
  if (!reviewRubric.value || reviewRubric.value.kind !== 'homework') return []
  const roles = new Set((reviewRubric.value.attachments || []).map((item) => item.role))
  return [
    roles.has('criteria') ? null : '评分标准 Word',
    roles.has('reference') ? null : '原题与答案 PDF',
  ].filter(Boolean)
})

const reviewPendingCount = computed(() => {
  const group = reviewGroup.value
  if (!group) return null
  if (reviewClassId.value === 'all') return Math.max(0, group.result_count - group.graded_count)
  const row = (group.classes || []).find((item) => item.class_id === reviewClassId.value)
  if (!row) return 0
  return Math.max(0, (row.result_count || 0) - (row.graded_count || 0))
})

/** 'all' 表示不传这个筛选条件。 */
function classParam(value) {
  return value === 'all' || value === null || value === undefined ? undefined : value
}

const kindRubrics = computed(() => rubrics.value.filter((item) => item.kind === kind.value))
// 「评分结果」和「一键 AI 评审」里能挑的细则，只跟当前「类型」同一种，
// 免得把作业的细则套到实验报告上
const resultRubricOptions = computed(() => kindRubrics.value)
const isLab = computed(() => kind.value === 'lab_report')
const pendingCount = computed(
  () => results.value.items.filter((item) => item.status === 'pending' || item.status === 'failed').length,
)

function formatSize(bytes) {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

async function loadCourses() {
  const page = await listCourses({ page: 1, page_size: 200 })
  courses.value = page.items
  // 这门课是固定的，进来就默认选上，省得每次手动点一下
  if (!courses.value.some((item) => item.id === courseId.value)) {
    courseId.value = courses.value[0]?.id ?? null
  }
}

async function loadRubricOptions() {
  const page = await listRubrics({
    page: 1,
    page_size: 200,
    course_id: courseId.value || undefined,
  })
  rubrics.value = page.items
  const wanted = route.params.rubricId ? Number(route.params.rubricId) : null
  const target = wanted ? rubrics.value.find((item) => item.id === wanted) : null
  if (target) {
    kind.value = target.kind
    rubricId.value = target.id
    return
  }
  if (!rubricId.value || !rubrics.value.some((item) => item.id === rubricId.value)) {
    rubricId.value = kindRubrics.value[0]?.id ?? null
  }
}

async function loadCourseClasses() {
  if (!courseId.value) {
    courseClasses.value = []
    return
  }
  const page = await listCourseClasses(courseId.value, { page: 1, page_size: 200 })
  courseClasses.value = page.items
}

async function loadRubric() {
  if (!rubricId.value) {
    rubric.value = null
    return
  }
  loading.value = true
  try {
    rubric.value = await getRubric(rubricId.value)
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    loading.value = false
  }
}

/** 下面成绩卡自己的作业选择。 */
async function loadResultsRubric() {
  if (!resultsRubricId.value) {
    resultsRubric.value = null
    results.value = emptyPage()
    summaries.value = emptyPage()
    return
  }
  try {
    resultsRubric.value = await getRubric(resultsRubricId.value)
    await Promise.all([loadResults(1), loadSummaries(1)])
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function loadResults(targetPage = results.value.page) {
  if (!resultsRubricId.value) return
  try {
    results.value = await listResults(resultsRubricId.value, {
      page: targetPage,
      page_size: PAGE_SIZE,
      class_id: classParam(resultsClassId.value),
    })
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function loadSummaries(targetPage = 1) {
  if (!resultsRubricId.value) return
  try {
    summaries.value = await classSummary(resultsRubricId.value, { page: targetPage, page_size: 10 })
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function refreshResults() {
  await Promise.all([
    loadResultsRubric(),
    rubricId.value ? getRubric(rubricId.value).then((row) => (rubric.value = row)) : null,
  ])
  if (resultsRubricId.value) {
    await Promise.all([loadResults(results.value.page), loadSummaries(summaries.value.page)])
  }
}

async function restoreDownloadedResults() {
  if (!resultsRubricId.value) return
  restoringResults.value = true
  try {
    const result = await restoreRubricResults(resultsRubricId.value)
    ElMessage.success(result.message)
    await refreshResults()
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    restoringResults.value = false
  }
}

/** 只在进页面 / 换课程时给下面的成绩卡挑一个默认作业，之后两边各管各的。 */
async function seedResultsRubric(reset = false) {
  if (reset || !rubrics.value.some((item) => item.id === resultsRubricId.value)) {
    resultsRubricId.value = rubricId.value
  }
  await loadResultsRubric()
}

async function onCourseChange() {
  rubricId.value = null
  resultsRubricId.value = null
  resultsClassId.value = 'all'
  await Promise.all([loadCourseClasses(), loadRubricOptions()])
  await loadRubric()
  await seedResultsRubric(true)
  if (rubricId.value) router.replace(`/grading/${rubricId.value}`)
}

async function onKindChange() {
  rubricId.value = kindRubrics.value[0]?.id ?? null
  await loadRubric()
  await seedResultsRubric(true)
}

async function onRubricChange() {
  if (rubricId.value) router.replace(`/grading/${rubricId.value}`)
  await loadRubric()
}

function onResultsRubricChange() {
  resultsClassId.value = 'all'
  loadResultsRubric()
}

async function pollUntilDone() {
  polling.value = true
  for (let i = 0; i < 60; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1500))
    await loadResults(results.value.page)
    if (!results.value.items.some((item) => item.status === 'grading')) break
  }
  polling.value = false
  await refreshResults()
  ElMessage.success('评审完成')
}

/** 打开「一键 AI 评审」：在这里挑这次作业用哪一份评分细则。 */
async function openReviewDialog() {
  if (!resultsRubricId.value) {
    ElMessage.warning('请先在下面「评分结果」里选择一次作业')
    return
  }
  reviewRubricId.value = resultsRubricId.value
  reviewClassId.value = 'all'
  reviewForce.value = false
  reviewDialog.value = true
  reviewLoading.value = true
  try {
    // 这里故意不带班级/状态筛选，才能按细则找到这次作业 / 这次实验报告
    const page = isLab.value
      ? await listIstudyLabReports({ course_id: courseId.value })
      : await listIstudyWorks({ course_id: courseId.value })
    reviewWorks.value = page.items
    if (!reviewPendingCount.value) reviewForce.value = true
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    reviewLoading.value = false
  }
}

async function submitReview() {
  if (!reviewRubricId.value) {
    ElMessage.warning('请选择一份评分细则')
    return
  }
  reviewing.value = true
  try {
    const group = reviewGroup.value
    const workId = group?.classes?.[0]?.work_id ?? null
    const payload = {
      rubric_id: reviewRubricId.value,
      class_id: reviewClassId.value === 'all' ? null : reviewClassId.value,
      force: reviewForce.value,
    }
    const run = workId
      ? group?.source === 'lab'
        ? await gradeIstudyLabReport(workId, payload)
        : await gradeIstudyWork(workId, payload)
      : await runGrading(reviewRubricId.value, {
          course_id: courseId.value,
          class_id: payload.class_id,
          force: payload.force,
        })

    if (run.rubric_id && run.rubric_id !== resultsRubricId.value) {
      // 换了细则：这次作业的结果已经改挂过去，下面的成绩卡跟着切
      // （顶部的作业选择器是独立的，不去动它）
      resultsRubricId.value = run.rubric_id
      router.replace(`/grading/${run.rubric_id}`)
    }
    reviewDialog.value = false
    if (!run.queued) {
      ElMessage.info(run.message)
    } else {
      ElMessage.success(run.message)
    }
    await Promise.all([loadRubric(), loadResultsRubric()])
    if (run.queued) pollUntilDone()
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    reviewing.value = false
  }
}

async function runOne(row) {
  try {
    await runSingleGrading(row.id)
    ElMessage.success(`已提交评审：${row.student_name}`)
    await loadResults(results.value.page)
    pollUntilDone()
  } catch (error) {
    ElMessage.error(error.message)
  }
}

function openResult(row) {
  current.value = row
  editForm.score = row.score
  editForm.comment = row.comment || ''
  drawerVisible.value = true
  loadResultImages(row.id)
}

/** 复核时把这次作业的图片一并调出来：题面 + 这位学生的作答，跟喂给模型的是同一批。 */
async function loadResultImages(resultId) {
  currentImages.value = []
  imagesLoading.value = true
  try {
    currentImages.value = await getResultImages(resultId)
  } catch (error) {
    currentImages.value = []
  } finally {
    imagesLoading.value = false
  }
}

function resultImageList() {
  return currentImages.value.map((item) => fileDownloadUrl(item.object_key))
}

async function saveResult() {
  if (!current.value) return
  savingResult.value = true
  try {
    await updateResult(current.value.id, { score: editForm.score, comment: editForm.comment })
    ElMessage.success('已保存（标记为人工调整）')
    drawerVisible.value = false
    await refreshResults()
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    savingResult.value = false
  }
}

async function removeResult(row) {
  await ElMessageBox.confirm(`删除 ${row.student_name} 的评分结果？`, '警告', { type: 'warning' })
  try {
    await deleteResult(row.id)
    ElMessage.success('已删除')
    await refreshResults()
  } catch (error) {
    ElMessage.error(error.message)
  }
}

function download(key) {
  window.open(fileDownloadUrl(key), '_blank')
}

function isScoreMismatch(row) {
  return (
    row.istudy_score != null &&
    row.score != null &&
    Number(row.istudy_score) !== Number(row.score)
  )
}

// ---------- 把本地分数 / 评语回填到 i学习 ----------
const backfillDialog = ref(false)
const backfillGateDialog = ref(false)
const backfillGateSeconds = ref(3)
const backfillWork = ref(null)
const backfillFinding = ref(false)
const backfillLoading = ref(false)
const backfillApplying = ref(false)
const backfillRows = ref([])
const backfillSummary = ref({ total: 0, write: 0, skip: 0 })
const backfillOverwrite = ref(false)
const backfillWriteComment = ref(true)
const backfillProgress = ref({ done: 0, total: 0 })
const backfillReceipts = ref([])
let backfillGateTimer = null

const backfillWorkId = computed(
  () => backfillWork.value?.classes?.[0]?.work_id || null,
)
const backfillWritable = computed(() =>
  backfillRows.value.filter((row) => row.action === 'write' && row.student_id),
)
const backfillPercent = computed(() => {
  const total = backfillProgress.value.total
  return total ? Math.round((backfillProgress.value.done / total) * 100) : 0
})

function backfillActionMeta(action) {
  if (action === 'write') return { label: '写入', type: 'success' }
  if (action === 'done') return { label: '已写入', type: 'success' }
  if (action === 'failed') return { label: '失败', type: 'danger' }
  return { label: '跳过', type: 'info' }
}

function stopBackfillGateTimer() {
  if (backfillGateTimer) {
    clearInterval(backfillGateTimer)
    backfillGateTimer = null
  }
  backfillGateSeconds.value = 3
}

function openBackfillGate() {
  if (!resultsRubricId.value || !courseId.value) return
  if (!resultsRubric.value?.stats?.graded_count) {
    ElMessage.warning('当前作业还没有可回填的评审成绩')
    return
  }
  stopBackfillGateTimer()
  backfillGateDialog.value = true
  backfillGateTimer = setInterval(() => {
    if (backfillGateSeconds.value <= 1) {
      backfillGateSeconds.value = 0
      if (backfillGateTimer) {
        clearInterval(backfillGateTimer)
        backfillGateTimer = null
      }
      return
    }
    backfillGateSeconds.value -= 1
  }, 1000)
}

async function confirmBackfillGate() {
  if (backfillGateSeconds.value > 0) return
  stopBackfillGateTimer()
  backfillGateDialog.value = false
  await openBackfillForRubric()
}

async function openBackfill(row) {
  backfillWork.value = row
  backfillDialog.value = true
  backfillOverwrite.value = false
  backfillWriteComment.value = true
  backfillReceipts.value = []
  backfillProgress.value = { done: 0, total: 0 }
  await loadBackfillPreview()
}

async function openBackfillForRubric() {
  if (!resultsRubricId.value || !courseId.value) return
  backfillFinding.value = true
  try {
    const page = await listIstudyWorks({ course_id: courseId.value })
    const row = (page.items || []).find((item) =>
      (item.rubric_ids || []).includes(resultsRubricId.value),
    )
    if (!row) {
      ElMessage.warning('没有找到这份评分细则对应的 i学习 作业')
      return
    }
    await openBackfill(row)
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    backfillFinding.value = false
  }
}

async function loadBackfillPreview() {
  if (!backfillWorkId.value) return
  backfillLoading.value = true
  try {
    const classes = backfillWork.value?.classes || []
    const plan = await previewIstudyBackfill(backfillWorkId.value, {
      overwrite: backfillOverwrite.value,
      // 卡片按班级筛选时，这行只剩一个班；没筛选时一次覆盖这次作业的所有班
      class_id: classes.length === 1 ? classes[0].class_id ?? undefined : undefined,
    })
    backfillRows.value = plan.rows || []
    backfillSummary.value = plan.summary || { total: 0, write: 0, skip: 0 }
  } catch (error) {
    backfillRows.value = []
    backfillSummary.value = { total: 0, write: 0, skip: 0 }
    ElMessage.error(error.message)
  } finally {
    backfillLoading.value = false
  }
}

async function runBackfill() {
  const targets = backfillWritable.value
  if (!targets.length || !backfillWorkId.value) return
  const scope = backfillWriteComment.value ? '分数和评语' : '分数'
  const extra = backfillOverwrite.value ? '（已批阅的会被覆盖）' : ''
  try {
    await ElMessageBox.confirm(
      `将把 ${targets.length} 名学生的${scope}写入 i学习${extra}，确定继续？`,
      '确认回填',
      { type: 'warning', confirmButtonText: '开始写入' },
    )
  } catch {
    return
  }

  backfillApplying.value = true
  backfillReceipts.value = []
  backfillProgress.value = { done: 0, total: targets.length }
  const chunkSize = 5
  try {
    for (let index = 0; index < targets.length; index += chunkSize) {
      const chunk = targets.slice(index, index + chunkSize)
      const result = await applyIstudyBackfill(backfillWorkId.value, {
        student_ids: chunk.map((row) => row.student_id),
        overwrite: backfillOverwrite.value,
        write_comment: backfillWriteComment.value,
      })
      const receipts = result.receipts || []
      backfillReceipts.value.push(...receipts)
      for (const receipt of receipts) {
        const row = backfillRows.value.find((item) => item.student_id === receipt.student_id)
        if (!row) continue
        row.action = receipt.ok ? 'done' : 'failed'
        row.backfill_status = receipt.ok ? 'success' : 'failed'
        row.reason = receipt.message
      }
      backfillProgress.value = {
        done: Math.min(index + chunk.length, targets.length),
        total: targets.length,
      }
    }
    const okCount = backfillReceipts.value.filter((item) => item.ok).length
    const failedCount = backfillReceipts.value.length - okCount
    if (failedCount) {
      ElMessage.warning(`回填完成：成功 ${okCount} 人，失败 ${failedCount} 人`)
    } else {
      ElMessage.success(`回填完成：成功写入 ${okCount} 人`)
    }
    await refreshResults()
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    backfillApplying.value = false
  }
}

function exportFile(format) {
  const params = {}
  if (courseId.value) params.course_id = courseId.value
  if (classParam(resultsClassId.value)) params.class_id = classParam(resultsClassId.value)
  const url = format === 'csv' ? exportCsvUrl(rubricId.value, params) : exportXlsxUrl(rubricId.value, params)
  window.open(url, '_blank')
}

watch(resultsClassId, () => {
  loadResults(1)
})

onMounted(async () => {
  try {
    await loadCourses()
    await loadRubricOptions()
    await loadCourseClasses()
    await loadRubric()
    await seedResultsRubric(true)
  } catch (error) {
    ElMessage.error(error.message)
  }
})

onBeforeUnmount(stopBackfillGateTimer)
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">评审与成绩</h2>
      </div>
      <div style="display: flex; gap: 8px">
        <el-button :disabled="!rubricId" @click="exportFile('csv')">导出 CSV</el-button>
        <el-button type="primary" :disabled="!rubricId" @click="exportFile('xlsx')">
          一键导出成绩与评语
        </el-button>
      </div>
    </div>

    <div class="table-card" style="margin-bottom: 16px">
      <div class="toolbar" style="margin-bottom: 0">
        <span>课程</span>
        <el-select
          v-model="courseId"
          placeholder="全部课程"
          clearable
          filterable
          style="width: 220px"
          @change="onCourseChange"
        >
          <el-option v-for="item in courses" :key="item.id" :label="item.name" :value="item.id" />
        </el-select>
        <span>类型</span>
        <el-select v-model="kind" style="width: 140px" @change="onKindChange">
          <el-option label="作业" value="homework" />
          <el-option label="实验报告" value="lab_report" />
        </el-select>
        <span>作业</span>
        <el-select
          v-model="rubricId"
          placeholder="选择评分细则"
          filterable
          style="width: 300px"
          @change="onRubricChange"
        >
          <el-option v-for="item in kindRubrics" :key="item.id" :label="item.name" :value="item.id" />
        </el-select>
        <el-tag v-if="!kindRubrics.length" type="info">这门课下还没有作业</el-tag>
        <el-tag v-else-if="rubric?.source === 'manual'" type="info">手工建的</el-tag>
      </div>
    </div>

    <div class="stat-grid" v-if="rubric">
      <div class="stat-card">
        <div class="label">评分结果</div>
        <div class="value">{{ rubric.stats.result_count }}</div>
      </div>
      <div class="stat-card">
        <div class="label">已评审</div>
        <div class="value metric-positive">{{ rubric.stats.graded_count }}</div>
      </div>
      <div class="stat-card">
        <div class="label">待评审</div>
        <div class="value metric-pending">{{ rubric.stats.pending_count }}</div>
      </div>
      <div class="stat-card">
        <div class="label">涉及班级</div>
        <div class="value">{{ rubric.stats.class_count }}</div>
      </div>
      <div class="stat-card">
        <div class="label">平均分</div>
        <div class="value">{{ rubric.stats.average_score ?? '—' }}</div>
      </div>
    </div>

    <template v-if="rubric">
      <div class="table-card">
        <div class="toolbar">
          <span style="font-weight: 600">评分结果</span>
          <el-select
            v-model="resultsRubricId"
            placeholder="选择作业"
            filterable
            style="width: 220px"
            @change="onResultsRubricChange"
          >
            <el-option
              v-for="item in resultRubricOptions"
              :key="item.id"
              :label="item.name"
              :value="item.id"
            />
          </el-select>
          <el-select
            v-model="resultsClassId"
            style="width: 220px"
          >
            <el-option label="全部班级" value="all" />
            <el-option
              v-for="item in courseClasses"
              :key="item.id"
              :label="item.full_name"
              :value="item.id"
            />
          </el-select>
          <el-button @click="refreshResults">刷新</el-button>
          <el-button
            v-if="resultsRubric?.source?.startsWith('istudy') && !resultsRubric?.stats?.result_count"
            :loading="restoringResults"
            @click="restoreDownloadedResults"
          >
            恢复已下载提交
          </el-button>
          <el-tag v-if="polling" type="warning">评审中…</el-tag>
          <el-button type="primary" :disabled="!resultsRubricId" @click="openReviewDialog">
            {{
              pendingCount
                ? `一键 AI 评审（本页 ${pendingCount} 条待评）`
                : '一键 AI 评审 / 重新评审'
            }}
          </el-button>
          <el-button
            v-if="resultsRubric?.kind === 'homework'"
            :loading="backfillFinding"
            :disabled="!resultsRubric?.stats?.graded_count"
            @click="openBackfillGate"
          >
            回填到 i学习
          </el-button>
        </div>

        <el-table :data="results.items" empty-text="还没有评分结果">
          <el-table-column prop="class_name" label="班级" width="100" />
          <el-table-column prop="student_no" label="学号" width="120" />
          <el-table-column prop="student_name" label="姓名" width="80" />
          <el-table-column
            prop="source_filename"
            label="提交文件"
            min-width="150"
            show-overflow-tooltip
          />
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="statusMeta[row.status]?.type">
                {{ statusMeta[row.status]?.label || row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="总分" width="80" align="center" header-align="center">
            <template #default="{ row }">{{ row.istudy_score ?? '-' }}</template>
          </el-table-column>
          <el-table-column label="AI评审" width="90" align="center" header-align="center">
            <template #default="{ row }">
              <span
                :style="isScoreMismatch(row) ? 'color: #e8590c; font-weight: 600' : ''"
              >
                {{ row.score ?? '-' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column prop="comment" label="评语" min-width="180" show-overflow-tooltip />
          <el-table-column label="操作" width="210" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openResult(row)">复核</el-button>
              <el-button link @click="runOne(row)">重新评审</el-button>
              <el-button link @click="download(row.object_key)">下载</el-button>
              <el-button link type="danger" @click="removeResult(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="pager">
          <el-pagination
            layout="total, prev, pager, next"
            :current-page="results.page"
            :page-size="results.page_size"
            :total="results.total"
            @current-change="loadResults"
          />
        </div>
      </div>

      <div class="table-card" style="margin-top: 16px" v-if="summaries.items.length">
        <div style="font-weight: 600; margin-bottom: 12px">班级汇总</div>
        <el-table :data="summaries.items" size="small">
          <el-table-column prop="full_name" label="班级" min-width="220" />
          <el-table-column prop="total" label="结果数" width="90" />
          <el-table-column prop="graded" label="已评审" width="90" />
          <el-table-column prop="pending" label="待评审" width="90" />
          <el-table-column label="平均分" width="90">
            <template #default="{ row }">{{ row.average_score ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="最高 / 最低" width="130">
            <template #default="{ row }">{{ row.max_score ?? '—' }} / {{ row.min_score ?? '—' }}</template>
          </el-table-column>
        </el-table>
        <div class="pager">
          <el-pagination
            small
            layout="prev, pager, next"
            :current-page="summaries.page"
            :page-size="summaries.page_size"
            :total="summaries.total"
            @current-change="loadSummaries"
          />
        </div>
      </div>
    </template>

    <el-empty
      v-if="!courseId"
      description="请先选择课程"
    />
    <el-empty
      v-else-if="!kindRubrics.length"
      :description="`这门课下还没有可评阅的${isLab ? '实验报告' : '作业'}细则`"
    />
    <el-empty
      v-else-if="!rubric"
      description="请选择作业"
    />

    <el-drawer v-model="drawerVisible" title="评分复核" size="760px">
      <template v-if="current">
        <el-descriptions :column="2" border size="small" style="margin-bottom: 16px">
          <el-descriptions-item label="学生">
            {{ current.student_name }}（{{ current.student_no }}）
          </el-descriptions-item>
          <el-descriptions-item label="班级">{{ current.class_name }}</el-descriptions-item>
          <el-descriptions-item label="文件">{{ current.source_filename }}</el-descriptions-item>
          <el-descriptions-item label="大小">{{ formatSize(current.size_bytes) }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ statusMeta[current.status]?.label }}</el-descriptions-item>
          <el-descriptions-item label="模型">{{ current.model || '—' }}</el-descriptions-item>
          <el-descriptions-item label="本次用量">
            <span v-if="current.images_sent">
              {{ current.images_sent }} 张图 · 输入 {{ current.prompt_tokens ?? '—' }} /
              输出 {{ current.completion_tokens ?? '—' }} token
            </span>
            <span v-else class="muted">—</span>
          </el-descriptions-item>
          <el-descriptions-item label="匹配依据" :span="2">
            {{ current.match_reason || '—' }}
          </el-descriptions-item>
        </el-descriptions>

        <div style="font-weight: 600; margin-bottom: 8px">作业图片</div>
        <div v-loading="imagesLoading" style="min-height: 40px; margin-bottom: 18px">
          <div v-if="currentImages.length" style="display: flex; flex-wrap: wrap; gap: 10px">
            <div
              v-for="(image, index) in currentImages"
              :key="image.object_key"
              style="text-align: center"
            >
              <el-image
                :src="fileDownloadUrl(image.object_key)"
                :preview-src-list="resultImageList()"
                :initial-index="index"
                fit="cover"
                lazy
                preview-teleported
                style="width: 104px; height: 104px; border-radius: 6px; border: 1px solid #e6eaf0"
              />
              <div class="muted" style="font-size: 12px; margin-top: 4px">
                {{ image.role === 'question' ? '题目' : '作答' }}
              </div>
            </div>
          </div>
          <div v-else-if="!imagesLoading" class="muted">没有可显示的图片（多半是没抓过附件）</div>
        </div>

        <el-alert
          v-if="current.error_message"
          type="error"
          :closable="false"
          :title="current.error_message"
          style="margin-bottom: 16px"
        />

        <div style="font-weight: 600; margin-bottom: 8px">总分（满分 {{ rubric?.total_score }}）</div>
        <el-input-number v-model="editForm.score" :min="0" :max="rubric?.total_score || 100" />

        <div style="font-weight: 600; margin: 16px 0 8px">评语</div>
        <el-input v-model="editForm.comment" type="textarea" :rows="8" />

        <div style="margin-top: 20px; display: flex; gap: 8px">
          <el-button type="primary" :loading="savingResult" @click="saveResult">保存修改</el-button>
          <el-button @click="drawerVisible = false">关闭</el-button>
        </div>
      </template>
    </el-drawer>

    <el-dialog v-model="reviewDialog" title="一键 AI 评审" width="640px">
      <div v-loading="reviewLoading">
        <div style="font-weight: 600; margin-bottom: 8px">作业</div>
        <el-input :model-value="reviewRubric?.name || ''" disabled />

        <div style="font-weight: 600; margin: 18px 0 8px">本次评审用哪一份评分细则</div>
        <el-select v-model="reviewRubricId" filterable style="width: 100%">
          <el-option
            v-for="item in resultRubricOptions"
            :key="item.id"
            :label="item.name"
            :value="item.id"
          />
        </el-select>
        <div
          v-if="reviewGroup && reviewRubricId !== resultsRubricId"
          style="margin-top: 6px; color: #e8590c; font-size: 13px"
        >
          更换后，本次作业已有结果会改挂到新细则
        </div>

        <div style="font-weight: 600; margin: 18px 0 8px">评审范围</div>
        <el-select v-model="reviewClassId" style="width: 100%">
          <el-option label="这次作业的全部班级" value="all" />
          <el-option
            v-for="item in courseClasses"
            :key="item.id"
            :label="item.full_name"
            :value="item.id"
          />
        </el-select>

        <el-checkbox v-model="reviewForce" style="margin-top: 14px">
          已评审过的也重新评一遍
        </el-checkbox>

        <el-alert
          type="info"
          :closable="false"
          style="margin-top: 16px"
          :title="
            reviewPendingCount === null
              ? '还没抓过附件'
              : `本次将评审 ${reviewPendingCount} 条记录（满分 ${reviewRubric?.total_score ?? 100}）`
          "
        />
        <el-alert
          v-if="reviewMissingMaterials.length"
          type="warning"
          :closable="false"
          style="margin-top: 12px"
          :title="`评分细则缺少：${reviewMissingMaterials.join('、')}。请先到评分细则页补齐材料。`"
        />
      </div>

      <template #footer>
        <el-button @click="reviewDialog = false">取消</el-button>
        <el-button
          type="primary"
          :loading="reviewing"
          :disabled="
            !reviewRubricId ||
            reviewMissingMaterials.length ||
            (!reviewForce && !reviewPendingCount)
          "
          @click="submitReview"
        >
          开始评审
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="backfillGateDialog"
      title="确认回填到 i学习"
      width="560px"
      :close-on-click-modal="false"
      @closed="stopBackfillGateTimer"
    >
      <el-alert
        type="warning"
        :closable="false"
        title="这是写操作，确认后会把这次作业的成绩和评语回传到 i学习。"
      />
      <div style="margin-top: 18px; line-height: 1.8">
        <div>
          即将回填的作业：<strong>{{ resultsRubric?.name || '当前作业' }}</strong>
        </div>
        <div class="muted">
          确认后将先进入这次作业的回填预览。请核对学生、本地分数和 i学习
          上这次作业的现有分数，再执行最终写入。
        </div>
      </div>
      <div
        style="
          margin-top: 16px;
          padding: 12px 14px;
          color: #985f0d;
          background: #fff8e8;
          border: 1px solid #f5dfb1;
          border-radius: 10px;
          font-size: 12px;
        "
      >
        {{
          backfillGateSeconds > 0
            ? `请仔细阅读以上说明，${backfillGateSeconds} 秒后才能继续。`
            : '倒计时已结束，现在可以确认并进入回填预览。'
        }}
      </div>
      <template #footer>
        <el-button @click="backfillGateDialog = false">取消</el-button>
        <el-button
          type="danger"
          :disabled="backfillGateSeconds > 0"
          @click="confirmBackfillGate"
        >
          {{ backfillGateSeconds > 0 ? `请等待 ${backfillGateSeconds} 秒` : '确认并继续' }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="backfillDialog" title="一键回填到 i学习" width="900px">
      <div v-loading="backfillLoading">
        <el-alert
          type="warning"
          :closable="false"
          style="margin-bottom: 12px"
          title="会把本地评审结果写入 i学习 作业批阅，学生端立即可见。请先核对分数与评语。"
        />
        <div style="display: flex; align-items: center; gap: 12px; margin-bottom: 12px">
          <span style="font-weight: 600">{{ backfillWork?.name }}</span>
          <el-checkbox v-model="backfillOverwrite" @change="loadBackfillPreview">
            覆盖 i学习 已批阅的
          </el-checkbox>
          <el-checkbox v-model="backfillWriteComment">同时写入 AI 评语</el-checkbox>
          <el-button link @click="loadBackfillPreview">刷新</el-button>
          <span class="muted" style="margin-left: auto">
            共 {{ backfillSummary.total }} 人 · 将写入 {{ backfillSummary.write }} 人 · 跳过
            {{ backfillSummary.skip }} 人
          </span>
        </div>

        <el-table :data="backfillRows" size="small" max-height="430" empty-text="没有可回填的学生">
          <el-table-column prop="class_name" label="班级" width="110" />
          <el-table-column prop="student_no" label="学号" width="125" />
          <el-table-column prop="student_name" label="姓名" width="90" />
          <el-table-column label="本地分数" width="90">
            <template #default="{ row }">{{ row.local_score ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="i学习分数" width="100">
            <template #default="{ row }">
              <span
                :style="
                  row.istudy_score != null &&
                  row.local_score != null &&
                  row.istudy_score !== row.local_score
                    ? 'color:#e8590c;font-weight:600'
                    : ''
                "
              >
                {{ row.istudy_score ?? '—' }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="评语" min-width="180" show-overflow-tooltip>
            <template #default="{ row }">{{ row.local_comment || '—' }}</template>
          </el-table-column>
          <el-table-column prop="istudy_state" label="i学习状态" width="140" />
          <el-table-column label="动作" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="backfillActionMeta(row.action).type">
                {{ backfillActionMeta(row.action).label }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="reason" label="说明" min-width="190" show-overflow-tooltip />
        </el-table>

        <el-progress
          v-if="backfillApplying || backfillProgress.total"
          :percentage="backfillPercent"
          :status="backfillApplying ? undefined : 'success'"
          style="margin-top: 14px"
        />
      </div>

      <template #footer>
        <el-button @click="backfillDialog = false">关闭</el-button>
        <el-button
          type="primary"
          :loading="backfillApplying"
          :disabled="!backfillWritable.length"
          @click="runBackfill"
        >
          开始写入（{{ backfillWritable.length }} 人）
        </el-button>
      </template>
    </el-dialog>

  </div>
</template>
