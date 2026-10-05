<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  PAGE_SIZE,
  classSummary,
  compareIstudyLabStudents,
  compareIstudyStudents,
  deleteResult,
  exportCsvUrl,
  exportIstudyLabReport,
  exportXlsxUrl,
  exportIstudyWork,
  fileDownloadUrl,
  gradeIstudyLabReport,
  gradeIstudyWork,
  getIstudyExport,
  getResultImages,
  getRubric,
  listCourseClasses,
  listCourses,
  listIstudyLabReports,
  listIstudyLabSubmissions,
  listIstudySubmissions,
  listIstudyWorks,
  listResults,
  listRubrics,
  runGrading,
  runSingleGrading,
  syncIstudyLabReports,
  syncIstudyWorks,
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

// ---------- i学习 作业 ----------
const works = ref({ items: [], total: 0, last_synced_at: null })
const worksLoading = ref(false)
const syncingWorks = ref(false)
const workStatusFilter = ref('all')
// i学习 作业卡片自己的班级筛选，跟下面的成绩表互不影响
const worksClassId = ref('all')

const workDialog = ref(false)
const workContext = ref(null)
// 这个弹窗既服务「作业」也服务「实验报告」：work 走 i学习 打包，lab 是直接下载
const workSource = ref('work')
const selectedWorkIds = ref([])
const exportContent = ref(0)
const exportFmt = ref(1)
const exportForce = ref(false)
const exportJobs = ref([])
const exporting = ref(false)
const exportStartedAt = ref(0)

// ---------- i学习 实验报告 ----------
const labReports = ref({ items: [], total: 0, last_synced_at: null })
const labLoading = ref(false)
const syncingLab = ref(false)
const labStatusFilter = ref('all')
const labClassId = ref('all')

// 按人抓取：work_id -> 选中的学生（i学习 user id）。空 / 不存在 = 整班抓
const pickMap = ref({})
const studentDialog = ref(false)
const studentLoading = ref(false)
const studentContext = ref(null)
const studentRows = ref([])
const studentChecked = ref([])

const studentStateMeta = {
  new: { label: '还没抓', type: 'warning' },
  fetched: { label: '已抓过', type: 'success' },
  not_submitted: { label: '未交', type: 'info' },
  not_in_roster: { label: '名单里没有', type: 'danger' },
}

const submissionsDialog = ref(false)
const submissionsTitle = ref('')
const submissions = ref([])
const submissionsLoading = ref(false)

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

const reviewPendingCount = computed(() => {
  const group = reviewGroup.value
  if (!group) return null
  if (reviewClassId.value === 'all') return Math.max(0, group.result_count - group.graded_count)
  const row = (group.classes || []).find((item) => item.class_id === reviewClassId.value)
  if (!row) return 0
  return Math.max(0, (row.result_count || 0) - (row.graded_count || 0))
})

const workStatusMeta = {
  not_started: { label: '未开始', type: 'info' },
  ongoing: { label: '进行中', type: 'warning' },
  ended: { label: '已结束', type: 'success' },
}

/** 'all' 表示不传这个筛选条件。 */
function classParam(value) {
  return value === 'all' || value === null || value === undefined ? undefined : value
}

const exportStatusMeta = {
  queued: { label: '排队中', type: 'info' },
  running: { label: '正在下载', type: 'warning' },
  exporting: { label: 'i学习打包中', type: 'warning' },
  downloading: { label: '下载中', type: 'warning' },
  parsing: { label: '解包匹配中', type: 'warning' },
  done: { label: '已完成', type: 'success' },
  failed: { label: '失败', type: 'danger' },
}

const kindRubrics = computed(() => rubrics.value.filter((item) => item.kind === kind.value))
// 「评分结果」和「一键 AI 评审」里能挑的细则，只跟当前「类型」同一种，
// 免得把作业的细则套到实验报告上
const resultRubricOptions = computed(() => kindRubrics.value)
const pendingCount = computed(
  () => results.value.items.filter((item) => item.status === 'pending' || item.status === 'failed').length,
)

function formatSize(bytes) {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function formatDateTime(value) {
  return value ? String(value).replace('T', ' ').slice(5, 16) : '—'
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
  workStatusFilter.value = 'all'
  worksClassId.value = 'all'
  labStatusFilter.value = 'all'
  labClassId.value = 'all'
  await Promise.all([loadCourseClasses(), loadRubricOptions(), loadScrapeList()])
  await loadRubric()
  await seedResultsRubric(true)
  if (rubricId.value) router.replace(`/grading/${rubricId.value}`)
}

async function onKindChange() {
  rubricId.value = kindRubrics.value[0]?.id ?? null
  await loadScrapeList()
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
    await Promise.all([loadWorks(), loadRubric(), loadResultsRubric()])
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

// ---------- i学习 作业 ----------
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

async function loadWorks() {
  if (!courseId.value || kind.value !== 'homework') {
    works.value = { items: [], total: 0, last_synced_at: null }
    return
  }
  worksLoading.value = true
  try {
    works.value = await listIstudyWorks({
      course_id: courseId.value,
      class_id: classParam(worksClassId.value),
      status: workStatusFilter.value === 'all' ? undefined : workStatusFilter.value,
    })
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    worksLoading.value = false
  }
}

async function loadLabReports() {
  if (!courseId.value || kind.value !== 'lab_report') {
    labReports.value = { items: [], total: 0, last_synced_at: null }
    return
  }
  labLoading.value = true
  try {
    labReports.value = await listIstudyLabReports({
      course_id: courseId.value,
      class_id: classParam(labClassId.value),
      status: labStatusFilter.value === 'all' ? undefined : labStatusFilter.value,
    })
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    labLoading.value = false
  }
}

/** 「i学习」那张卡片：作业和实验报告共用一套筛选和表格，只是取数接口不同。 */
const isLab = computed(() => kind.value === 'lab_report')
const scrapeList = computed(() => (isLab.value ? labReports.value : works.value))
const scrapeLoading = computed(() => (isLab.value ? labLoading.value : worksLoading.value))
const scrapeTitle = computed(() => (isLab.value ? 'i学习 实验报告' : 'i学习 作业'))
const scrapeClassId = computed({
  get: () => (isLab.value ? labClassId.value : worksClassId.value),
  set: (value) => {
    if (isLab.value) labClassId.value = value
    else worksClassId.value = value
  },
})
const scrapeStatus = computed({
  get: () => (isLab.value ? labStatusFilter.value : workStatusFilter.value),
  set: (value) => {
    if (isLab.value) labStatusFilter.value = value
    else workStatusFilter.value = value
  },
})

function loadScrapeList() {
  return isLab.value ? loadLabReports() : loadWorks()
}

async function syncScrapeList() {
  if (!courseId.value) return
  if (isLab.value) {
    syncingLab.value = true
    try {
      const result = await syncIstudyLabReports({ course_id: courseId.value })
      ElMessage.success(result.message)
      await loadLabReports()
    } catch (error) {
      ElMessage.error(error.message)
    } finally {
      syncingLab.value = false
    }
    return
  }
  await syncWorks()
}

const syncingScrape = computed(() => (isLab.value ? syncingLab.value : syncingWorks.value))

async function syncWorks() {
  if (!courseId.value) return
  syncingWorks.value = true
  try {
    const result = await syncIstudyWorks({ course_id: courseId.value })
    ElMessage.success(result.message)
    if (result.notes?.length) ElMessage.warning(result.notes.join('；'))
    await loadWorks()
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    syncingWorks.value = false
  }
}

function workGroupStatus(row) {
  if (row.status) return workStatusMeta[row.status]
  const parts = Object.entries(row.status_breakdown || {}).map(
    ([key, count]) => `${workStatusMeta[key]?.label || key} ${count}`,
  )
  return { label: parts.join(' / ') || '—', type: 'info' }
}

function openWorkDialog(row) {
  workSource.value = row.source || 'work'
  workContext.value = row
  // 已经抓过的班级默认不勾，避免重复抓浪费时间；想重抓手动勾上就行。
  // 实验报告的「表单型」报告本来就没有附件，也默认不勾。
  selectedWorkIds.value = (row.classes || [])
    .filter(
      (item) => !item.captured_count && (workSource.value !== 'lab' || item.submitted_count > 0),
    )
    .map((item) => item.work_id)
  exportContent.value = 0
  exportFmt.value = 1
  exportForce.value = false
  exportJobs.value = []
  exportStartedAt.value = 0
  pickMap.value = {}
  workDialog.value = true
}

/** 打开某个班的「按人抓取」明细：对比 i学习 已交名单和本地已抓。 */
async function openStudents(row) {
  const source = row.source || workSource.value
  studentContext.value = { work_id: row.work_id, class_name: row.class_name, source }
  studentDialog.value = true
  studentLoading.value = true
  studentRows.value = []
  studentChecked.value = []
  try {
    const data =
      source === 'lab'
        ? await compareIstudyLabStudents(row.work_id)
        : await compareIstudyStudents(row.work_id)
    studentRows.value = data.students
    const saved = pickMap.value[row.work_id]
    studentChecked.value = saved?.length
      ? [...saved]
      : data.students
          .filter((item) => item.state === 'new' && item.istudy_user_id)
          .map((item) => item.istudy_user_id)
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    studentLoading.value = false
  }
}

function pickableStudents() {
  // 只有 i学习 那边确实交了作业的人才能按人导出
  return studentRows.value.filter((item) => item.istudy_user_id)
}

function selectNewStudents() {
  studentChecked.value = studentRows.value
    .filter((item) => item.state === 'new' && item.istudy_user_id)
    .map((item) => item.istudy_user_id)
}

function confirmStudents() {
  const workId = studentContext.value?.work_id
  if (!workId) return
  const next = { ...pickMap.value }
  if (studentChecked.value.length) {
    next[workId] = [...studentChecked.value]
  } else {
    delete next[workId] // 没选人就当整班抓
  }
  pickMap.value = next
  studentDialog.value = false
}

function pickedCount(workId) {
  return pickMap.value[workId]?.length || 0
}

const pendingClassCount = computed(
  () => (workContext.value?.classes || []).filter((item) => !item.captured_count).length,
)

const fetchedClassCount = computed(
  () => (workContext.value?.classes || []).filter((item) => item.captured_count).length,
)

const skipHint = computed(() => {
  if (!fetchedClassCount.value) return ''
  if (!pendingClassCount.value) {
    return `${workContext.value?.classes?.length || 0} 个班都已抓过，要重抓请勾选`
  }
  return `${fetchedClassCount.value} 个班已抓过，本次跳过；要重抓请勾选`
})

function selectAllClasses() {
  selectedWorkIds.value = (workContext.value?.classes || []).map((item) => item.work_id)
}

function selectOnlyPending() {
  selectedWorkIds.value = (workContext.value?.classes || [])
    .filter((item) => !item.captured_count)
    .map((item) => item.work_id)
}

function jobOf(workId) {
  return exportJobs.value.find((item) => item.work_id === workId)
}

async function waitForExport(job) {
  // 实验报告是同步下载（不用去 i学习 的下载中心排队），所以直接看这条报告的进度
  if (job.source === 'lab') {
    for (let i = 0; i < 200; i += 1) {
      await sleep(2000)
      try {
        const page = await listIstudyLabReports({ course_id: courseId.value })
        const row = (page.items || [])
          .flatMap((group) => group.classes || [])
          .find((item) => item.work_id === job.work_id)
        if (!row) continue
        // 这次没提交任何抓取任务（都抓过了 / 没人交），i学习 那边什么也不会发生
        if (row.sync_state === 'idle') {
          job.status = 'done'
          return
        }
        job.status = row.sync_state
        job.message = row.sync_message
        if (row.sync_state === 'done' || row.sync_state === 'failed') return
      } catch (error) {
        job.status = 'failed'
        job.message = error.message
        return
      }
    }
    job.status = 'failed'
    job.message = '等待超时'
    return
  }
  // i学习 打包是异步的，一个班实测要三分钟左右
  for (let i = 0; i < 300; i += 1) {
    await sleep(4000)
    let row
    try {
      row = await getIstudyExport(job.export_id)
    } catch (error) {
      job.status = 'failed'
      job.message = error.message
      return
    }
    job.status = row.status
    job.message = row.message
    if (row.status === 'done' || row.status === 'failed') return
  }
  job.status = 'failed'
  job.message = '等待超时'
}

async function runExport() {
  if (!selectedWorkIds.value.length) {
    ElMessage.warning('请至少选择一个班级')
    return
  }
  exporting.value = true
  exportStartedAt.value = Date.now()
  const picked = workContext.value.classes.filter((item) =>
    selectedWorkIds.value.includes(item.work_id),
  )
  exportJobs.value = picked.map((item) => ({
    work_id: item.work_id,
    class_name: item.class_name,
    source: workSource.value,
    export_id: null,
    status: 'queued',
    message: '准备提交',
  }))

  // 1) 先把所有班的导出都提交上去，i学习 那边会并行打包
  await Promise.all(
    exportJobs.value.map(async (job) => {
      try {
        const picked = pickMap.value[job.work_id] || []
        if (job.source === 'lab') {
          const created = await exportIstudyLabReport(job.work_id, {
            student_ids: picked.length ? picked : null,
            force: picked.length ? false : exportForce.value,
          })
          job.status = created.queued ? 'running' : 'done'
          job.message = created.queued
            ? picked.length
              ? `正在从 i学习 下载（只抓选中的 ${picked.length} 人）`
              : `正在从 i学习 下载（${created.queued} 人）`
            : created.message
          return
        }
        const created = await exportIstudyWork(job.work_id, {
          // 按人抓取时后端会强制用「仅提交附件」：i学习 的按人导出配 PDF 会卡住
          content: picked.length ? 1 : exportContent.value,
          fmt: exportFmt.value,
          person_ids: picked.length ? picked : null,
        })
        job.export_id = created.id
        job.status = created.status
        job.message = picked.length
          ? `已提交给 i学习（只抓选中的 ${picked.length} 人）`
          : created.message || '已提交给 i学习'
      } catch (error) {
        job.status = 'failed'
        job.message = error.message
      }
    }),
  )

  // 2) 再一起轮询进度
  await Promise.all(
    // 作业是异步打包，要拿 export_id 去轮询；实验报告是同步下载，直接看这条报告的进度
    exportJobs.value
      .filter((job) => job.export_id || job.source === 'lab')
      .map((job) => waitForExport(job)),
  )

  exporting.value = false
  const done = exportJobs.value.filter((job) => job.status === 'done').length
  if (done === exportJobs.value.length) {
    ElMessage.success(`已完成 ${done} 个班的附件抓取`)
  } else {
    ElMessage.warning(`完成 ${done} / ${exportJobs.value.length} 个班，失败的可以重试`)
  }
  await loadScrapeList()
}

async function openSubmissions(workId, className, source = 'work') {
  submissionsDialog.value = true
  submissionsLoading.value = true
  submissions.value = []
  submissionsTitle.value = `${workContext.value?.name || ''} · ${className}`
  try {
    submissions.value =
      source === 'lab'
        ? await listIstudyLabSubmissions(workId)
        : await listIstudySubmissions(workId)
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    submissionsLoading.value = false
  }
}

function imagePreviewList(row) {
  return (row.image_keys || []).map((key) => fileDownloadUrl(key))
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
watch(workStatusFilter, () => loadWorks())
watch(worksClassId, () => loadWorks())
watch(labStatusFilter, () => loadLabReports())
watch(labClassId, () => loadLabReports())

onMounted(async () => {
  try {
    await loadCourses()
    await loadRubricOptions()
    await loadCourseClasses()
    await loadScrapeList()
    await loadRubric()
    await seedResultsRubric(true)
  } catch (error) {
    ElMessage.error(error.message)
  }
})
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">评审与成绩</h2>
        <div class="page-subtitle">
          选课程 → 选作业 → 抓取附件 → 一键 AI 评审 → 导出成绩与评语
        </div>
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
        <div class="value" style="color: #2f9e44">{{ rubric.stats.graded_count }}</div>
      </div>
      <div class="stat-card">
        <div class="label">待评审</div>
        <div class="value" style="color: #e8590c">{{ rubric.stats.pending_count }}</div>
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

    <div class="table-card" style="margin-bottom: 16px" v-if="courseId">
      <div class="toolbar">
        <span style="font-weight: 600">{{ scrapeTitle }}</span>
        <el-select v-model="scrapeClassId" style="width: 170px">
          <el-option label="全部班级" value="all" />
          <el-option
            v-for="item in courseClasses"
            :key="item.id"
            :label="item.full_name"
            :value="item.id"
          />
        </el-select>
        <el-select v-model="scrapeStatus" style="width: 150px">
          <el-option label="全部状态" value="all" />
          <el-option label="未开始" value="not_started" />
          <el-option label="进行中" value="ongoing" />
          <el-option label="已结束" value="ended" />
        </el-select>
        <el-button @click="loadScrapeList">刷新</el-button>
        <el-button type="primary" :loading="syncingScrape" @click="syncScrapeList">
          从 i学习 同步{{ isLab ? '实验报告' : '作业' }}
        </el-button>
        <span class="muted" v-if="scrapeList.last_synced_at">
          上次同步 {{ formatDateTime(scrapeList.last_synced_at) }}
        </span>
      </div>

      <el-table
        :data="scrapeList.items"
        v-loading="scrapeLoading"
        :empty-text="`还没有${isLab ? '实验报告' : '作业'}`"
      >
        <el-table-column :label="isLab ? '实验报告' : '作业'" min-width="130">
          <template #default="{ row }">
            <el-tooltip
              :content="`作答时间：${formatDateTime(row.start_at)} ~ ${formatDateTime(row.end_at)}`"
              placement="top"
            >
              <span>{{ row.name }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column label="班级" width="120">
          <template #default="{ row }">
            <el-popover v-if="row.class_count > 1" placement="right" trigger="hover" width="240">
              <template #reference>
                <span style="cursor: pointer">{{ row.class_count }} 个班</span>
              </template>
              <div v-for="item in row.classes" :key="item.work_id" style="line-height: 24px">
                {{ item.class_name }}
                <el-tag
                  v-if="row.source === 'lab' && item.report_type !== 2"
                  size="small"
                  type="info"
                  style="margin-left: 4px"
                >
                  表单型
                </el-tag>
                <span class="muted">
                  （{{ workStatusMeta[item.status]?.label || item.status }} · 已交
                  {{ item.submitted_count }}）
                </span>
              </div>
            </el-popover>
            <span v-else>
              {{ row.classes[0]?.class_name || '—' }}
              <el-tag
                v-if="row.source === 'lab' && row.classes[0]?.report_type !== 2"
                size="small"
                type="info"
              >
                表单型
              </el-tag>
            </span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="workGroupStatus(row).type">
              {{ workGroupStatus(row).label }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="i学习 已交/未交" width="140">
          <template #default="{ row }">
            {{ row.submitted_count }} / {{ row.unsubmitted_count }}
          </template>
        </el-table-column>
        <el-table-column label="抓取进度" min-width="185">
          <template #default="{ row }">
            <span
              :class="{ muted: !row.captured_count }"
              style="white-space: nowrap"
            >已抓 {{ row.captured_count }} 人 / 待抓 {{ Math.max(0, row.submitted_count - row.captured_count) }} 人</span>
          </template>
        </el-table-column>
        <el-table-column label="待评审" width="85">
          <template #default="{ row }">
            {{ Math.max(0, (row.result_count || 0) - (row.graded_count || 0)) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openWorkDialog(row)">抓取附件</el-button>
            <el-button
              link
              :disabled="!row.captured_count"
              @click="
                openSubmissions(row.classes[0].work_id, row.classes[0].class_name, row.source)
              "
            >
              明细
            </el-button>
          </template>
        </el-table-column>
      </el-table>
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
          <el-tag v-if="polling" type="warning">评审中…</el-tag>
          <el-button type="primary" :disabled="!pendingCount" @click="openReviewDialog">
            一键 AI 评审（本页 {{ pendingCount }} 条待评）
          </el-button>
        </div>

        <el-table :data="results.items" empty-text="还没有评分结果">
          <el-table-column prop="class_name" label="班级" width="120" />
          <el-table-column prop="student_no" label="学号" width="140" />
          <el-table-column prop="student_name" label="姓名" width="90" />
          <el-table-column
            prop="source_filename"
            label="提交文件"
            min-width="200"
            show-overflow-tooltip
          />
          <el-table-column label="状态" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="statusMeta[row.status]?.type">
                {{ statusMeta[row.status]?.label || row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="总分" width="80">
            <template #default="{ row }">{{ row.score ?? '—' }}</template>
          </el-table-column>
          <el-table-column prop="comment" label="评语" min-width="220" show-overflow-tooltip />
          <el-table-column label="操作" width="240" fixed="right">
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
      v-else-if="!scrapeList.items.length && !scrapeLoading"
      :description="`还没有${isLab ? '实验报告' : '作业'}`"
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
      </div>

      <template #footer>
        <el-button @click="reviewDialog = false">取消</el-button>
        <el-button
          type="primary"
          :loading="reviewing"
          :disabled="!reviewRubricId || (!reviewForce && !reviewPendingCount)"
          @click="submitReview"
        >
          开始评审
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="studentDialog"
      :title="`按人抓取 · ${studentContext?.class_name || ''}`"
      width="720px"
    >
      <div v-loading="studentLoading">
        <el-alert
          type="info"
          :closable="false"
          style="margin-bottom: 12px"
          title="对比 i学习 已交名单与本地已抓，勾选后只导出这些人"
        />
        <div style="margin-bottom: 8px">
          <el-button link type="primary" @click="selectNewStudents">只勾还没抓的</el-button>
          <el-button link @click="studentChecked = []">全不选</el-button>
          <el-button link @click="studentChecked = pickableStudents().map((s) => s.istudy_user_id)">
            全选可导出的
          </el-button>
          <span class="muted" style="margin-left: 8px">
            已选 {{ studentChecked.length }} 人
          </span>
        </div>
        <el-table :data="studentRows" size="small" max-height="420">
          <el-table-column width="46">
            <template #default="{ row }">
              <el-checkbox
                :model-value="studentChecked.includes(row.istudy_user_id)"
                :disabled="!row.istudy_user_id"
                @change="(checked) => {
                  const next = new Set(studentChecked)
                  if (checked) next.add(row.istudy_user_id)
                  else next.delete(row.istudy_user_id)
                  studentChecked = [...next]
                }"
              />
            </template>
          </el-table-column>
          <el-table-column prop="student_no" label="学号" width="120" />
          <el-table-column prop="name" label="姓名" width="100" />
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="studentStateMeta[row.state]?.type">
                {{ studentStateMeta[row.state]?.label || row.state }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="submitted_at" label="提交时间" width="130" />
        </el-table>
        <div class="muted" style="margin-top: 10px">
          「未交」和「名单里没有」的无法导出
        </div>
      </div>

      <template #footer>
        <el-button @click="studentDialog = false">取消</el-button>
        <el-button type="primary" @click="confirmStudents">
          确定（{{ studentChecked.length ? `只抓 ${studentChecked.length} 人` : '改回整班抓' }}）
        </el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="workDialog"
      :title="`抓取${workSource === 'lab' ? '实验报告' : '作业附件'} · ${workContext?.name || ''}`"
      width="840px"
    >
      <el-steps
        :active="exportJobs.length ? (exporting ? 1 : 2) : 0"
        simple
        style="margin-bottom: 18px"
      >
        <el-step title="选择范围" />
        <el-step title="抓取附件" />
      </el-steps>

      <template v-if="workContext">
        <template v-if="!exportJobs.length">
          <div
            style="
              display: flex;
              align-items: center;
              justify-content: space-between;
              margin-bottom: 10px;
            "
          >
            <span style="font-weight: 600">班级</span>
            <span>
              <el-button link type="primary" @click="selectOnlyPending">只选没抓过的</el-button>
              <el-button link @click="selectAllClasses">全选（含重抓）</el-button>
            </span>
          </div>
          <el-checkbox-group v-model="selectedWorkIds">
            <el-checkbox
              v-for="item in workContext.classes"
              :key="item.work_id"
              :value="item.work_id"
              style="display: block; margin-bottom: 6px"
            >
              {{ item.class_name }}
              <el-tag
                v-if="workSource === 'lab' && item.report_type !== 2"
                size="small"
                type="info"
                style="margin-left: 4px"
              >
                表单型（没有附件）
              </el-tag>
              <el-tag
                v-if="workSource === 'lab'"
                size="small"
                :type="workStatusMeta[item.status]?.type"
                style="margin-left: 4px"
              >
                {{ workStatusMeta[item.status]?.label || item.status }}
              </el-tag>
              <span v-if="item.captured_count" class="muted">
                （已抓 {{ item.captured_count }} 人 / 待抓
                {{ Math.max(0, item.submitted_count - item.captured_count) }} 人）
              </span>
              <span v-else class="muted">
                （已抓 0 人 / 待抓 {{ item.submitted_count }} 人）
              </span>
              <el-button link type="primary" style="margin-left: 8px" @click.stop="openStudents(item)">
                详情
              </el-button>
              <el-tag v-if="pickedCount(item.work_id)" size="small" type="warning">
                只抓 {{ pickedCount(item.work_id) }} 人
              </el-tag>
            </el-checkbox>
          </el-checkbox-group>

          <el-alert
            v-if="skipHint"
            type="success"
            :closable="false"
            style="margin-top: 12px"
            :title="skipHint"
          />
          <el-alert
            v-if="Object.keys(pickMap).length && workSource !== 'lab'"
            type="warning"
            :closable="false"
            style="margin-top: 10px"
            title="按人抓取的班级用「仅提交附件」格式"
          />

          <template v-if="workSource === 'lab'">
            <div style="font-weight: 600; margin: 18px 0 10px">抓取方式</div>
            <el-checkbox v-model="exportForce">已经抓过的学生也重新抓一遍</el-checkbox>
            <div class="muted" style="margin-top: 6px; font-size: 13px">
              默认只抓还没抓过的人，新交的那几个补一下就行。
            </div>
          </template>
          <template v-else>
            <div style="font-weight: 600; margin: 18px 0 10px">导出内容</div>
            <el-radio-group v-model="exportContent">
              <el-radio :value="0" style="display: block; margin-bottom: 6px">
                完整答题记录（PDF）
              </el-radio>
              <el-radio :value="1" style="display: block">
                仅学生提交的原始附件
              </el-radio>
            </el-radio-group>
          </template>

          <el-alert
            type="info"
            :closable="false"
            style="margin-top: 16px"
            :title="
              workSource === 'lab'
                ? '直接从 i学习 下载作答，约十几秒到一分钟，可关闭窗口，后台继续'
                : 'i学习 打包约 3 分钟，可关闭窗口，后台继续'
            "
          />
        </template>

        <template v-else>
          <el-alert
            :type="exporting ? 'warning' : 'success'"
            :closable="false"
            :title="
              exporting
                ? workSource === 'lab'
                  ? '正在从 i学习 下载作答…'
                  : '正在抓取，i学习 打包通常要几分钟…'
                : '抓取结束'
            "
            style="margin-bottom: 12px"
          />
          <el-table :data="exportJobs" size="small">
            <el-table-column prop="class_name" label="班级" width="150" />
            <el-table-column label="状态" width="140">
              <template #default="{ row }">
                <el-tag size="small" :type="exportStatusMeta[row.status]?.type">
                  {{ exportStatusMeta[row.status]?.label || row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="message" label="说明" min-width="300" show-overflow-tooltip />
            <el-table-column label="明细" width="90">
              <template #default="{ row }">
                <el-button
                  link
                  type="primary"
                  :disabled="row.status !== 'done'"
                  @click="openSubmissions(row.work_id, row.class_name, row.source)"
                >
                  查看
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </template>

      <template #footer>
        <el-button v-if="exportJobs.length && !exporting" @click="exportJobs = []">重新选择</el-button>
        <el-button @click="workDialog = false">关闭</el-button>
        <el-button
          v-if="!exportJobs.length"
          type="primary"
          :loading="exporting"
          :disabled="!selectedWorkIds.length"
          @click="runExport"
        >
          开始抓取（{{ selectedWorkIds.length }} 个班）
        </el-button>
      </template>
    </el-dialog>

    <el-drawer
      v-model="submissionsDialog"
      :title="`提交明细 · ${submissionsTitle}`"
      size="760px"
    >
      <div v-loading="submissionsLoading">
        <el-empty
          v-if="!submissionsLoading && !submissions.length"
          description="还没有抓过这次作业的附件"
        />
        <el-table v-else :data="submissions" size="small" max-height="660">
          <el-table-column prop="student_name" label="姓名" width="90" />
          <el-table-column prop="student_no" label="学号" width="120" />
          <el-table-column label="状态" width="80">
            <template #default="{ row }">
              <el-tag size="small" :type="row.state === 'missing' ? 'info' : 'success'">
                {{ row.state === 'missing' ? '未交' : '已交' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="作答图片" width="150">
            <template #default="{ row }">
              <template v-if="row.image_keys?.length">
                <el-image
                  v-for="(url, index) in imagePreviewList(row)"
                  :key="url"
                  :src="url"
                  :preview-src-list="imagePreviewList(row)"
                  :initial-index="index"
                  fit="cover"
                  preview-teleported
                  style="width: 34px; height: 34px; margin-right: 4px; border-radius: 4px"
                />
              </template>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column prop="source_filename" label="文件" min-width="200" show-overflow-tooltip />
          <el-table-column prop="error_message" label="备注" min-width="150" show-overflow-tooltip />
        </el-table>
      </div>
    </el-drawer>
  </div>
</template>
