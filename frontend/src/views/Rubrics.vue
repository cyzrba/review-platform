<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  PAGE_SIZE,
  createRubric,
  deleteRubric,
  listCourses,
  listRubrics,
  updateRubric,
} from '@/api'

const router = useRouter()
const loading = ref(false)
const page = ref({ items: [], total: 0, page: 1, page_size: PAGE_SIZE, pages: 0 })
const courses = ref([])
const filters = reactive({ kind: null, course_id: null, q: '' })

const dialog = ref(false)
const saving = ref(false)
const editingId = ref(null)
const form = reactive({
  name: '',
  kind: 'homework',
  description: '',
  criteria: '',
  extra_prompt: '',
  total_score: 100,
  course_ids: [],
})

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
  saving.value = true
  const payload = { ...form }
  try {
    if (editingId.value) {
      await updateRubric(editingId.value, payload)
      ElMessage.success('已保存')
      dialog.value = false
      await load(page.value.page)
    } else {
      const created = await createRubric(payload)
      ElMessage.success('评分细则已创建')
      dialog.value = false
      router.push(`/grading/${created.id}`)
    }
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    saving.value = false
  }
}

async function remove(row) {
  await ElMessageBox.confirm(
    `删除「${row.name}」会同时删掉它的 ${row.stats.result_count} 条评分结果，确定吗？`,
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
        <div class="page-subtitle">
          一个项目 / 一次作业对应一份评分细则，可以挂到一个或多个课程下；AI 只按它给一个总分和一段评语
        </div>
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
      <el-table :data="page.items" empty-text="还没有评分细则">
        <el-table-column prop="name" label="项目 / 作业" min-width="180" />
        <el-table-column label="类型" width="100">
          <template #default="{ row }">
            <el-tag size="small" :type="row.kind === 'lab_report' ? 'warning' : ''">
              {{ row.kind === 'lab_report' ? '实验报告' : '作业' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="所属课程" min-width="160">
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
        <el-table-column label="满分" width="70">
          <template #default="{ row }">{{ row.total_score }}</template>
        </el-table-column>
        <el-table-column label="结果" width="200">
          <template #default="{ row }">
            <span class="muted">
              {{ row.stats.result_count }} 条 · 已评审 {{ row.stats.graded_count }} ·
              平均 {{ row.stats.average_score ?? '—' }}
            </span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190" fixed="right">
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
        <el-form-item label="评分标准">
          <el-input
            v-model="form.criteria"
            type="textarea"
            :rows="7"
            placeholder="评审依据，写清给分点和扣分点，例如：&#10;1. 实验原理阐述正确（25 分）&#10;2. 步骤完整可复现（30 分）"
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
  </div>
</template>
