<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  PAGE_SIZE,
  createRubric,
  deleteRubric,
  deleteRubricMaterial,
  fileDownloadUrl,
  getRubricMaterialPreview,
  getRubric,
  listCourses,
  listRubrics,
  updateRubric,
  uploadRubricMaterial,
} from '@/api'

const router = useRouter()
const loading = ref(false)
const page = ref({ items: [], total: 0, page: 1, page_size: PAGE_SIZE, pages: 0 })
const courses = ref([])
const filters = reactive({ kind: null, course_id: null, q: '' })

const dialog = ref(false)
const saving = ref(false)
const editingId = ref(null)
const editingRow = ref(null)
const wordFile = ref(null)
const pdfFile = ref(null)
const materialPreviewDialog = ref(false)
const materialPreview = ref(null)
const materialPreviewLoading = ref(false)
const form = reactive({
  name: '',
  kind: 'homework',
  description: '',
  criteria: '',
  extra_prompt: '',
  total_score: 100,
  course_ids: [],
})

function material(row, role) {
  return (row?.attachments || []).find((item) => item.role === role) || null
}

function formatFileSize(bytes) {
  if (!bytes) return '0 B'
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function selectMaterial(uploadFile, role) {
  const raw = uploadFile?.raw || null
  if (!raw) return
  if (role === 'criteria') wordFile.value = raw
  else pdfFile.value = raw
}

function downloadMaterial(item) {
  if (item?.object_key) window.open(fileDownloadUrl(item.object_key), '_blank')
}

async function previewMaterial(role) {
  if (!editingId.value) return
  materialPreviewDialog.value = true
  materialPreviewLoading.value = true
  materialPreview.value = null
  try {
    materialPreview.value = await getRubricMaterialPreview(editingId.value, role)
  } catch (error) {
    materialPreviewDialog.value = false
    ElMessage.error(error.message)
  } finally {
    materialPreviewLoading.value = false
  }
}

async function loadCourseOptions() {
  const data = await listCourses({ page: 1, page_size: 200 })
  courses.value = data.items
}

async function load(targetPage = 1) {
  loading.value = true
  try {
    page.value = await listRubrics({
      page: targetPage,
      page_size: PAGE_SIZE,
      kind: filters.kind || undefined,
      course_id: filters.course_id || undefined,
      q: filters.q || undefined,
    })
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    loading.value = false
  }
}

function openCreate() {
  editingId.value = null
  editingRow.value = null
  wordFile.value = null
  pdfFile.value = null
  Object.assign(form, {
    name: '',
    kind: 'homework',
    description: '',
    criteria: '',
    extra_prompt: '',
    total_score: 100,
    course_ids: filters.course_id ? [filters.course_id] : [],
  })
  dialog.value = true
}

function openEdit(row) {
  editingId.value = row.id
  editingRow.value = row
  wordFile.value = null
  pdfFile.value = null
  Object.assign(form, {
    name: row.name,
    kind: row.kind,
    description: row.description || '',
    criteria: row.criteria || '',
    extra_prompt: row.extra_prompt || '',
    total_score: row.total_score,
    course_ids: (row.courses || []).map((item) => item.id),
  })
  dialog.value = true
}

async function submit() {
  if (!form.name.trim()) {
    ElMessage.warning('请填写项目 / 作业名称')
    return
  }
  const hasWord = Boolean(wordFile.value || material(editingRow.value, 'criteria'))
  const hasPdf = Boolean(pdfFile.value || material(editingRow.value, 'reference'))
  if (form.kind === 'homework' && (!hasWord || !hasPdf)) {
    ElMessage.warning('作业评分细则必须同时上传评分标准 Word 和原题与答案 PDF')
    return
  }
  saving.value = true
  const payload = { ...form }
  const creating = !editingId.value
  try {
    let rubricId = editingId.value
    if (editingId.value) {
      await updateRubric(editingId.value, payload)
    } else {
      const created = await createRubric(payload)
      rubricId = created.id
      editingId.value = rubricId
      editingRow.value = created
    }
    if (wordFile.value) {
      await uploadRubricMaterial(rubricId, 'criteria', wordFile.value)
    }
    if (pdfFile.value) {
      await uploadRubricMaterial(rubricId, 'reference', pdfFile.value)
    }
    ElMessage.success(creating ? '评分细则已创建' : '已保存')
    dialog.value = false
    if (creating) {
      router.push(`/grading/${rubricId}`)
    } else {
      await load(page.value.page)
    }
  } catch (error) {
    ElMessage.error(`保存失败：${error.message}`)
  } finally {
    saving.value = false
  }
}

async function removeMaterial(role) {
  if (!editingId.value) return
  const label = role === 'criteria' ? '评分标准 Word' : '原题与答案 PDF'
  await ElMessageBox.confirm(`删除当前的${label}？`, '确认删除', { type: 'warning' })
  try {
    await deleteRubricMaterial(editingId.value, role)
    editingRow.value = await getRubric(editingId.value)
    ElMessage.success('材料已删除')
    await load(page.value.page)
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function remove(row) {
  await ElMessageBox.confirm(
    `删除「${row.name}」？如果它已经被作业使用或已有评分结果，系统会拒绝删除以保护数据。`,
    '警告',
    { type: 'warning' },
  )
  try {
    await deleteRubric(row.id)
    ElMessage.success('已删除')
    await load(page.value.page)
  } catch (error) {
    ElMessage.error(error.message)
  }
}

onMounted(async () => {
  try {
    await loadCourseOptions()
  } catch (error) {
    ElMessage.error(error.message)
  }
  await load()
})
</script>

<template>
  <div class="page" v-loading="loading">
    <div class="page-header">
      <div>
        <h2 class="page-title">评分细则</h2>
      </div>
      <el-button type="primary" @click="openCreate">新建评分细则</el-button>
    </div>

    <div class="toolbar">
      <el-select v-model="filters.course_id" placeholder="全部课程" clearable style="width: 220px" @change="load(1)">
        <el-option v-for="item in courses" :key="item.id" :label="item.name" :value="item.id" />
      </el-select>
      <el-select v-model="filters.kind" placeholder="全部类型" clearable style="width: 150px" @change="load(1)">
        <el-option label="作业" value="homework" />
        <el-option label="实验报告" value="lab_report" />
      </el-select>
      <el-input
        v-model="filters.q"
        placeholder="按名称搜索"
        clearable
        style="width: 220px"
        @keyup.enter="load(1)"
        @clear="load(1)"
      />
      <el-button @click="load(1)">查询</el-button>
    </div>

    <div class="table-card">
      <el-table :data="page.items" empty-text="还没有评分细则" class="rubric-table">
        <el-table-column prop="name" label="项目 / 作业" min-width="180" align="center" header-align="center" />
        <el-table-column label="类型" width="100" align="center" header-align="center">
          <template #default="{ row }">
            <el-tag size="small" :type="row.kind === 'lab_report' ? 'warning' : ''">
              {{ row.kind === 'lab_report' ? '实验报告' : '作业' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="所属课程" min-width="160" align="center" header-align="center">
          <template #default="{ row }">
            <el-tag
              v-for="course in row.courses"
              :key="course.id"
              size="small"
              style="margin-right: 6px"
            >
              {{ course.name }}
            </el-tag>
            <span v-if="!row.courses?.length" class="muted">未挂课程</span>
          </template>
        </el-table-column>
        <el-table-column label="材料" width="150" align="center" header-align="center">
          <template #default="{ row }">
            <div class="rubric-material-tags">
              <el-tag
                size="small"
                :type="material(row, 'criteria') ? 'success' : 'info'"
              >
                Word
              </el-tag>
              <el-tag
                size="small"
                :type="material(row, 'reference') ? 'success' : 'info'"
              >
                PDF
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="满分" width="70" align="center" header-align="center">
          <template #default="{ row }">{{ row.total_score }}</template>
        </el-table-column>
        <el-table-column label="结果" width="250" align="center" header-align="center">
          <template #default="{ row }">
            <span class="muted rubric-result-summary">
              {{ row.stats.result_count }} 条 · 已评审 {{ row.stats.graded_count }} ·
              平均 {{ row.stats.average_score ?? '—' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190" fixed="right" align="center" header-align="center">
          <template #default="{ row }">
            <el-button link type="primary" @click="router.push(`/grading/${row.id}`)">去评审</el-button>
            <el-button link @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" @click="remove(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div class="pager">
        <el-pagination
          layout="total, prev, pager, next"
          :current-page="page.page"
          :page-size="page.page_size"
          :total="page.total"
          @current-change="load"
        />
      </div>
    </div>

    <el-dialog
      v-model="dialog"
      :title="editingId ? '编辑评分细则' : '新建评分细则'"
      width="720px"
      top="6vh"
    >
      <el-form label-width="110px">
        <el-form-item label="名称">
          <el-input v-model="form.name" placeholder="如 实验一 数据采集 / 第 3 次作业" />
        </el-form-item>
        <el-form-item label="所属课程">
          <el-select v-model="form.course_ids" multiple placeholder="可多选" style="width: 100%">
            <el-option v-for="item in courses" :key="item.id" :label="item.name" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="类型">
          <el-radio-group v-model="form.kind">
            <el-radio value="homework">作业</el-radio>
            <el-radio value="lab_report">实验报告</el-radio>
          </el-radio-group>
          <div class="muted" style="margin-left: 12px">
            {{
              form.kind === 'lab_report'
                ? '实验报告：压缩包里是 学生名字_学号.zip，里面是 学生名字_学号.pdf'
                : '作业：只认压缩包里的 Word 文件，文件名 院系-专业-班级名称-学号-学生名称'
            }}
          </div>
        </el-form-item>
        <el-form-item label="评分标准 Word">
          <div class="rubric-material-editor">
            <div v-if="material(editingRow, 'criteria')" class="rubric-current-material">
              <div>
                <strong>{{ material(editingRow, 'criteria').filename }}</strong>
                <span>{{ formatFileSize(material(editingRow, 'criteria').size_bytes) }}</span>
              </div>
              <el-button link type="primary" @click="previewMaterial('criteria')">预览</el-button>
              <el-button link type="primary" @click="downloadMaterial(material(editingRow, 'criteria'))">
                下载
              </el-button>
              <el-button link type="danger" @click="removeMaterial('criteria')">删除</el-button>
            </div>
            <div class="rubric-upload-row">
              <el-upload
                :auto-upload="false"
                :show-file-list="false"
                accept=".docx"
                :on-change="(file) => selectMaterial(file, 'criteria')"
              >
                <el-button>{{ material(editingRow, 'criteria') ? '更换 Word' : '选择 Word' }}</el-button>
              </el-upload>
              <span v-if="wordFile" class="muted">
                待上传：{{ wordFile.name }}（{{ formatFileSize(wordFile.size) }}）
              </span>
              <span v-else class="muted">仅支持 .docx，保存时提取其中的评分标准文字</span>
            </div>
          </div>
        </el-form-item>
        <el-form-item label="原题与答案 PDF">
          <div class="rubric-material-editor">
            <div v-if="material(editingRow, 'reference')" class="rubric-current-material">
              <div>
                <strong>{{ material(editingRow, 'reference').filename }}</strong>
                <span>
                  {{ formatFileSize(material(editingRow, 'reference').size_bytes) }} ·
                  {{ material(editingRow, 'reference').page_count }} 页
                </span>
              </div>
              <el-button link type="primary" @click="previewMaterial('reference')">预览</el-button>
              <el-button link type="primary" @click="downloadMaterial(material(editingRow, 'reference'))">
                下载
              </el-button>
              <el-button link type="danger" @click="removeMaterial('reference')">删除</el-button>
            </div>
            <div class="rubric-upload-row">
              <el-upload
                :auto-upload="false"
                :show-file-list="false"
                accept=".pdf"
                :on-change="(file) => selectMaterial(file, 'reference')"
              >
                <el-button>{{ material(editingRow, 'reference') ? '更换 PDF' : '选择 PDF' }}</el-button>
              </el-upload>
              <span v-if="pdfFile" class="muted">
                待上传：{{ pdfFile.name }}（{{ formatFileSize(pdfFile.size) }}）
              </span>
              <span v-else class="muted">仅支持 PDF，保存时逐页渲染并作为题面和标准答案参考</span>
            </div>
          </div>
        </el-form-item>
        <el-form-item label="满分">
          <el-input-number v-model="form.total_score" :min="1" :max="1000" />
        </el-form-item>
        <el-form-item label="任务说明">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="3"
            placeholder="题目要求 / 本次任务要做什么，会一并交给 AI"
          />
        </el-form-item>
        <el-form-item label="补充评分说明">
          <el-input
            v-model="form.criteria"
            type="textarea"
            :rows="7"
            placeholder="可选。用于补充 Word 评分标准，例如重点关注项或统一扣分说明"
          />
        </el-form-item>
        <el-form-item label="附加提示词">
          <el-input
            v-model="form.extra_prompt"
            type="textarea"
            :rows="2"
            placeholder="可选，额外叮嘱 AI 的话，如：重点关注实验步骤的可复现性"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="submit">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="materialPreviewDialog"
      :title="`材料预览 · ${materialPreview?.filename || ''}`"
      width="940px"
      top="4vh"
    >
      <div v-loading="materialPreviewLoading" class="rubric-preview-body">
        <template v-if="materialPreview">
          <template v-if="materialPreview.role === 'criteria'">
            <el-alert
              type="info"
              :closable="false"
              title="Word 文档文本预览，评价格式可能与 Word 页面略有不同"
            />
            <pre class="rubric-word-preview">{{ materialPreview.extracted_text }}</pre>
          </template>
          <template v-else>
            <iframe
              class="rubric-pdf-preview"
              :src="fileDownloadUrl(materialPreview.object_key)"
              :title="materialPreview.filename"
            ></iframe>
          </template>
        </template>
      </div>
      <template #footer>
        <el-button
          v-if="materialPreview"
          @click="downloadMaterial(materialPreview)"
        >
          下载原件
        </el-button>
        <el-button type="primary" @click="materialPreviewDialog = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>
