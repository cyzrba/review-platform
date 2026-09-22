<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  PAGE_SIZE,
  classSummary,
  deleteResult,
  exportCsvUrl,
  exportXlsxUrl,
  fileDownloadUrl,
  getRubric,
  listCourseClasses,
  listCourses,
  listResults,
  listRubrics,
  runGrading,
  runSingleGrading,
  updateResult,
  uploadHint,
  uploadSubmissions,
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
const hint = ref(null)
const results = ref(emptyPage())
const summaries = ref(emptyPage())
const courseClasses = ref([])
const filterClassId = ref(null)
const uploadClassId = ref(null)
const polling = ref(false)

const uploadFiles = ref([])
const uploading = ref(false)
const summary = ref(null)

const drawerVisible = ref(false)
const current = ref(null)
const editForm = reactive({ score: null, comment: '' })
const savingResult = ref(false)

const statusMeta = {
  pending: { label: '待评审', type: 'info' },
  grading: { label: '评审中', type: 'warning' },
  graded: { label: '已评审', type: 'success' },
  failed: { label: '评审失败', type: 'danger' },
}

const kindRubrics = computed(() => rubrics.value.filter((item) => item.kind === kind.value))
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
    hint.value = null
    results.value = emptyPage()
    summaries.value = emptyPage()
    return
  }
  loading.value = true
  try {
    const [detail, hintInfo] = await Promise.all([
      getRubric(rubricId.value),
      uploadHint(rubricId.value),
    ])
    rubric.value = detail
    hint.value = hintInfo
    await Promise.all([loadResults(1), loadSummaries(1)])
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    loading.value = false
  }
}

async function loadResults(targetPage = results.value.page) {
  if (!rubricId.value) return
  try {
    results.value = await listResults(rubricId.value, {
      page: targetPage,
      page_size: PAGE_SIZE,
      class_id: filterClassId.value || undefined,
    })
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function loadSummaries(targetPage = 1) {
  if (!rubricId.value) return
  try {
    summaries.value = await classSummary(rubricId.value, { page: targetPage, page_size: 10 })
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function refreshResults() {
  if (!rubricId.value) return
  rubric.value = await getRubric(rubricId.value)
  await Promise.all([loadResults(results.value.page), loadSummaries(summaries.value.page)])
}

async function onCourseChange() {
  rubricId.value = null
  filterClassId.value = null
  uploadClassId.value = null
  await Promise.all([loadCourseClasses(), loadRubricOptions()])
  await loadRubric()
  if (rubricId.value) router.replace(`/grading/${rubricId.value}`)
}

async function onKindChange() {
  rubricId.value = kindRubrics.value[0]?.id ?? null
  await loadRubric()
}

async function onRubricChange() {
  summary.value = null
  uploadFiles.value = []
  filterClassId.value = null
  if (rubricId.value) router.replace(`/grading/${rubricId.value}`)
  await loadRubric()
}

function onFileChange(file, fileList) {
  uploadFiles.value = fileList.map((item) => item.raw)
}

async function submitUpload() {
  if (!uploadFiles.value.length) {
    ElMessage.warning('请选择压缩包或文件')
    return
  }
  uploading.value = true
  summary.value = null
  try {
    summary.value = await uploadSubmissions(rubricId.value, uploadFiles.value, {
      classId: uploadClassId.value,
      courseId: courseId.value,
    })
    ElMessage.success(summary.value.message)
    uploadFiles.value = []
    await refreshResults()
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    uploading.value = false
  }
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

async function runAll() {
  try {
    const payload = {}
    if (courseId.value) payload.course_id = courseId.value
    if (filterClassId.value) payload.class_id = filterClassId.value
    const run = await runGrading(rubricId.value, payload)
    if (!run.queued) {
      ElMessage.info(run.message)
      return
    }
    ElMessage.success(`${run.message}（${run.queued} 条）`)
    await loadResults(results.value.page)
    pollUntilDone()
  } catch (error) {
    ElMessage.error(error.message)
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

function exportFile(format) {
  const params = {}
  if (courseId.value) params.course_id = courseId.value
  if (filterClassId.value) params.class_id = filterClassId.value
  const url = format === 'csv' ? exportCsvUrl(rubricId.value, params) : exportXlsxUrl(rubricId.value, params)
  window.open(url, '_blank')
}

watch(filterClassId, () => loadResults(1))

onMounted(async () => {
  try {
    await loadCourses()
    await loadRubricOptions()
    await loadCourseClasses()
    await loadRubric()
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
          选课程 → 选类型 → 选评分细则 → 上传本批次压缩包 → 一键 AI 评审 → 导出成绩与评语
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
        <span>评分细则</span>
        <el-select
          v-model="rubricId"
          placeholder="选择评分细则"
          filterable
          style="width: 300px"
          @change="onRubricChange"
        >
          <el-option v-for="item in kindRubrics" :key="item.id" :label="item.name" :value="item.id" />
        </el-select>
        <el-tag v-if="!kindRubrics.length" type="info">
          该范围下还没有这个类型的评分细则
        </el-tag>
        <el-tag v-else-if="rubric" type="success">满分 {{ rubric.total_score }}</el-tag>
      </div>
    </div>

    <template v-if="rubric">
      <div class="stat-grid">
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

      <div class="table-card" style="margin-bottom: 16px">
        <div style="font-weight: 600; margin-bottom: 12px">上传提交</div>
        <el-alert
          type="info"
          :closable="false"
          style="margin-bottom: 12px"
          :title="`文件要求：${hint?.expect || ''}`"
        />
        <div class="split" style="grid-template-columns: 1fr 320px">
          <el-upload drag multiple :auto-upload="false" :on-change="onFileChange">
            <div style="padding: 16px 0">
              把压缩包（或单个文件）拖到这里，或点击选择；支持一次选多个
            </div>
          </el-upload>
          <div>
            <div class="muted" style="margin-bottom: 6px">限定匹配范围（可选）</div>
            <el-select
              v-model="uploadClassId"
              placeholder="课程下全部班级"
              clearable
              style="width: 100%"
            >
              <el-option
                v-for="item in courseClasses"
                :key="item.id"
                :label="item.full_name"
                :value="item.id"
              />
            </el-select>
            <div class="muted" style="margin-top: 10px">
              当前范围：{{ courses.find((c) => c.id === courseId)?.name || '全部课程' }}
            </div>
            <el-button
              type="primary"
              style="margin-top: 12px; width: 100%"
              :loading="uploading"
              :disabled="!uploadFiles.length"
              @click="submitUpload"
            >
              上传并匹配（{{ uploadFiles.length }} 个文件）
            </el-button>
          </div>
        </div>

        <template v-if="summary">
          <el-alert
            :type="summary.matched ? 'success' : 'warning'"
            :closable="false"
            style="margin-top: 14px"
            :title="summary.message"
          />
          <el-table
            v-if="summary.issues?.length"
            :data="summary.issues"
            size="small"
            style="margin-top: 10px"
            max-height="240"
          >
            <el-table-column prop="filename" label="未采用的文件" min-width="300" />
            <el-table-column prop="reason" label="原因" min-width="300" />
          </el-table>
        </template>
      </div>

      <div class="table-card">
        <div class="toolbar">
          <span style="font-weight: 600">评分结果</span>
          <el-select
            v-model="filterClassId"
            placeholder="课程下全部班级"
            clearable
            style="width: 220px"
          >
            <el-option
              v-for="item in courseClasses"
              :key="item.id"
              :label="item.full_name"
              :value="item.id"
            />
          </el-select>
          <el-button @click="refreshResults">刷新</el-button>
          <el-tag v-if="polling" type="warning">评审中…</el-tag>
          <el-button type="primary" :disabled="!pendingCount" @click="runAll">
            一键 AI 评审（本页 {{ pendingCount }} 条待评）
          </el-button>
        </div>

        <el-table :data="results.items" empty-text="还没有评分结果，先上传提交文件">
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

    <el-empty v-else description="请先选择课程与评分细则" />

    <el-drawer v-model="drawerVisible" title="评分复核" size="560px">
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
          <el-descriptions-item label="匹配依据" :span="2">
            {{ current.match_reason || '—' }}
          </el-descriptions-item>
        </el-descriptions>

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
  </div>
</template>
