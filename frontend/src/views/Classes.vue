<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  PAGE_SIZE,
  createClass,
  createCourse,
  createStudent,
  deleteClass,
  deleteCourse,
  deleteStudent,
  importRoster,
  linkClasses,
  listClasses,
  listCourseClasses,
  listCourses,
  listStudents,
  rosterTemplateUrl,
  unlinkClass,
  updateCourse,
} from '@/api'

const emptyPage = () => ({ items: [], total: 0, page: 1, page_size: PAGE_SIZE, pages: 0 })

const courses = ref(emptyPage())
const classes = ref(emptyPage())
const students = ref(emptyPage())
const loading = reactive({ courses: false, classes: false, students: false })
const courseQuery = ref('')
const courseId = ref(null)
const classId = ref(null)

const courseDialog = ref(false)
const courseForm = reactive({ id: null, name: '', code: '', term: '', description: '' })
const classDialog = ref(false)
const classForm = reactive({ department: '', major: '', name: '' })
const linkDialog = ref(false)
const linkOptions = ref([])
const linkSelection = ref([])
const linkSearching = ref(false)
const studentDialog = ref(false)
const studentForm = reactive({
  student_no: '',
  name: '',
  enrollment_year: null,
  joined_at: null,
})
const importDialog = ref(false)
const importFile = ref(null)
const importing = ref(false)
const importResult = ref(null)

const currentCourse = computed(() => courses.value.items.find((item) => item.id === courseId.value))
const currentClass = computed(() => classes.value.items.find((item) => item.id === classId.value))

function formatDate(value) {
  return value ? String(value).replace('T', ' ').slice(0, 10) : '—'
}

async function loadCourses(page = 1) {
  loading.courses = true
  try {
    courses.value = await listCourses({
      page,
      page_size: PAGE_SIZE,
      q: courseQuery.value || undefined,
    })
    if (!courses.value.items.some((item) => item.id === courseId.value)) {
      courseId.value = courses.value.items[0]?.id ?? null
      classId.value = null
      await loadClasses(1)
    }
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    loading.courses = false
  }
}

async function loadClasses(page = 1) {
  if (!courseId.value) {
    classes.value = emptyPage()
    students.value = emptyPage()
    classId.value = null
    return
  }
  loading.classes = true
  try {
    classes.value = await listCourseClasses(courseId.value, { page, page_size: PAGE_SIZE })
    if (!classes.value.items.some((item) => item.id === classId.value)) {
      classId.value = classes.value.items[0]?.id ?? null
    }
    await loadStudents(1)
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    loading.classes = false
  }
}

async function loadStudents(page = 1) {
  if (!classId.value) {
    students.value = emptyPage()
    return
  }
  loading.students = true
  try {
    students.value = await listStudents({
      class_id: classId.value,
      page,
      page_size: PAGE_SIZE,
    })
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    loading.students = false
  }
}

async function selectCourse(item) {
  courseId.value = item.id
  classId.value = null
  await loadClasses(1)
}

async function selectClass(item) {
  classId.value = item.id
  await loadStudents(1)
}

function openCourseDialog(item = null) {
  Object.assign(courseForm, {
    id: item?.id ?? null,
    name: item?.name ?? '',
    code: item?.code ?? '',
    term: item?.term ?? '',
    description: item?.description ?? '',
  })
  courseDialog.value = true
}

async function submitCourse() {
  if (!courseForm.name.trim()) {
    ElMessage.warning('请填写课程名称')
    return
  }
  const payload = {
    name: courseForm.name,
    code: courseForm.code || null,
    term: courseForm.term || null,
    description: courseForm.description || null,
  }
  try {
    if (courseForm.id) {
      await updateCourse(courseForm.id, payload)
      ElMessage.success('课程已更新')
      courseDialog.value = false
      await loadCourses(courses.value.page)
    } else {
      const created = await createCourse(payload)
      ElMessage.success('课程已创建，现在可以导入班级与学生')
      courseDialog.value = false
      await loadCourses(1)
      courseId.value = created.id
      await loadClasses(1)
    }
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function removeCourse(item) {
  await ElMessageBox.confirm(
    `删除课程「${item.name}」只会解除它和班级、评分细则的关联，不会删除班级和细则。确定吗？`,
    '提示',
    { type: 'warning' },
  )
  try {
    await deleteCourse(item.id)
    ElMessage.success('已删除')
    if (courseId.value === item.id) courseId.value = null
    await loadCourses(1)
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function submitClass() {
  if (!classForm.name.trim()) {
    ElMessage.warning('请填写班级名称')
    return
  }
  try {
    const created = await createClass({
      name: classForm.name,
      department: classForm.department || '',
      major: classForm.major || '',
    })
    await linkClasses(courseId.value, [created.id])
    ElMessage.success('班级已创建并挂到当前课程')
    classDialog.value = false
    Object.assign(classForm, { department: '', major: '', name: '' })
    await loadClasses(1)
    classId.value = created.id
    await loadStudents(1)
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function openLinkDialog() {
  linkDialog.value = true
  linkSelection.value = []
  await searchLinkable()
}

async function searchLinkable(keyword = '') {
  linkSearching.value = true
  try {
    const page = await listClasses({
      unlinked_course_id: courseId.value,
      q: keyword || undefined,
      page: 1,
      page_size: 50,
    })
    linkOptions.value = page.items
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    linkSearching.value = false
  }
}

async function submitLink() {
  if (!linkSelection.value.length) {
    ElMessage.warning('请选择要挂载的班级')
    return
  }
  try {
    const result = await linkClasses(courseId.value, linkSelection.value)
    ElMessage.success(result.message)
    linkDialog.value = false
    await loadClasses(1)
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function removeClassFromCourse(item) {
  await ElMessageBox.confirm(`把「${item.name}」从课程里移出？班级本身和学生都会保留。`, '提示', {
    type: 'warning',
  })
  try {
    await unlinkClass(courseId.value, item.id)
    ElMessage.success('已移出课程')
    await loadClasses(1)
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function removeClassEverywhere(item) {
  await ElMessageBox.confirm(
    `彻底删除班级「${item.full_name}」会连同它的学生和评分结果一起删除，确定吗？`,
    '警告',
    { type: 'warning' },
  )
  try {
    await deleteClass(item.id)
    ElMessage.success('已删除')
    await loadClasses(classes.value.page)
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function submitStudent() {
  if (!studentForm.student_no || !studentForm.name) {
    ElMessage.warning('学号和姓名必填')
    return
  }
  try {
    await createStudent({ ...studentForm, class_id: classId.value })
    ElMessage.success('学生已添加')
    studentDialog.value = false
    Object.assign(studentForm, { student_no: '', name: '', enrollment_year: null, joined_at: null })
    await loadStudents(1)
    await loadClasses(classes.value.page)
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function removeStudent(row) {
  await ElMessageBox.confirm(`删除学生「${row.name}」？`, '提示', { type: 'warning' })
  try {
    await deleteStudent(row.id)
    ElMessage.success('已删除')
    await loadStudents(students.value.page)
    await loadClasses(classes.value.page)
  } catch (error) {
    ElMessage.error(error.message)
  }
}

function openImportDialog() {
  importFile.value = null
  importResult.value = null
  importDialog.value = true
}

function onImportFile(file) {
  importFile.value = file.raw
  return false
}

async function submitImport() {
  if (!importFile.value) {
    ElMessage.warning('请选择名单文件（CSV 或 XLSX）')
    return
  }
  importing.value = true
  importResult.value = null
  try {
    importResult.value = await importRoster(importFile.value, courseId.value)
    ElMessage.success(importResult.value.message)
    await loadCourses(courses.value.page)
    await loadClasses(1)
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    importing.value = false
  }
}

onMounted(async () => {
  await loadCourses(1)
  await loadClasses(1)
})
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">班级与学生</h2>
        <div class="page-subtitle">
          先建课程 → 选中课程 → 导入班级与学生（名单列：学号/工号、姓名、院系、专业、班级、加入时间、入学年份）
        </div>
      </div>
      <el-button tag="a" :href="rosterTemplateUrl" target="_blank">下载名单模板</el-button>
    </div>

    <div class="split-3">
      <!-- 课程 -->
      <div class="table-card">
        <div class="panel-head">
          <span style="font-weight: 600">课程</span>
          <el-button size="small" @click="openCourseDialog()">新建课程</el-button>
        </div>
        <el-input
          v-model="courseQuery"
          placeholder="搜索课程名 / 代码"
          clearable
          size="small"
          style="margin-bottom: 10px"
          @keyup.enter="loadCourses(1)"
          @clear="loadCourses(1)"
        />
        <div v-loading="loading.courses">
          <el-empty v-if="!courses.items.length" description="还没有课程" :image-size="70" />
          <div
            v-for="item in courses.items"
            :key="item.id"
            class="list-card"
            :class="{ active: item.id === courseId }"
            @click="selectCourse(item)"
          >
            <div class="row">
              <span>{{ item.name }}</span>
              <el-dropdown trigger="click" @command="(cmd) => cmd === 'edit' ? openCourseDialog(item) : removeCourse(item)">
                <el-button link size="small" @click.stop>···</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="edit">编辑</el-dropdown-item>
                    <el-dropdown-item command="delete" divided>删除</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
            <div class="muted">
              {{ item.code || '无课程代码' }}{{ item.term ? ` · ${item.term}` : '' }}
            </div>
            <div class="muted">
              {{ item.class_count }} 个班级 · {{ item.student_count }} 名学生 · {{ item.rubric_count }} 份细则
            </div>
          </div>
        </div>
        <div class="pager">
          <el-pagination
            small
            layout="prev, pager, next"
            :current-page="courses.page"
            :page-size="courses.page_size"
            :total="courses.total"
            @current-change="loadCourses"
          />
        </div>
      </div>

      <!-- 班级 -->
      <div class="table-card">
        <div class="panel-head">
          <span style="font-weight: 600">班级</span>
          <div>
            <el-button size="small" :disabled="!courseId" @click="classDialog = true">新建</el-button>
            <el-button size="small" :disabled="!courseId" @click="openLinkDialog">挂载已有</el-button>
          </div>
        </div>
        <el-alert
          v-if="!courseId"
          type="info"
          :closable="false"
          title="请先在左边创建或选择一个课程"
        />
        <div v-else v-loading="loading.classes">
          <el-empty v-if="!classes.items.length" description="该课程下还没有班级" :image-size="70" />
          <div
            v-for="item in classes.items"
            :key="item.id"
            class="list-card"
            :class="{ active: item.id === classId }"
            @click="selectClass(item)"
          >
            <div class="row">
              <span>{{ item.name }}</span>
              <el-dropdown
                trigger="click"
                @command="(cmd) => cmd === 'unlink' ? removeClassFromCourse(item) : removeClassEverywhere(item)"
              >
                <el-button link size="small" @click.stop>···</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="unlink">移出该课程</el-dropdown-item>
                    <el-dropdown-item command="delete" divided>彻底删除班级</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
            <div class="muted">{{ item.department }} · {{ item.major || '未填专业' }}</div>
            <div class="muted">{{ item.student_count }} 人</div>
          </div>
        </div>
        <div class="pager">
          <el-pagination
            small
            layout="prev, pager, next"
            :current-page="classes.page"
            :page-size="classes.page_size"
            :total="classes.total"
            @current-change="loadClasses"
          />
        </div>
      </div>

      <!-- 学生 -->
      <div class="table-card">
        <div class="panel-head">
          <span style="font-weight: 600">
            {{ currentClass ? `${currentClass.name} · 学生名单` : '学生名单' }}
          </span>
          <div>
            <el-button size="small" type="primary" :disabled="!courseId" @click="openImportDialog">
              导入名单
            </el-button>
            <el-button size="small" :disabled="!classId" @click="studentDialog = true">添加学生</el-button>
          </div>
        </div>
        <el-table :data="students.items" v-loading="loading.students" empty-text="暂无学生">
          <el-table-column prop="student_no" label="学号 / 工号" width="160" />
          <el-table-column prop="name" label="姓名" width="100" />
          <el-table-column prop="enrollment_year" label="入学年份" width="110">
            <template #default="{ row }">{{ row.enrollment_year ?? '—' }}</template>
          </el-table-column>
          <el-table-column label="加入时间" width="120">
            <template #default="{ row }">{{ formatDate(row.joined_at) }}</template>
          </el-table-column>
          <el-table-column prop="email" label="邮箱" min-width="150" />
          <el-table-column label="操作" width="80">
            <template #default="{ row }">
              <el-button link type="danger" @click="removeStudent(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div class="pager">
          <el-pagination
            layout="total, prev, pager, next"
            :current-page="students.page"
            :page-size="students.page_size"
            :total="students.total"
            @current-change="loadStudents"
          />
        </div>
      </div>
    </div>

    <el-dialog v-model="courseDialog" :title="courseForm.id ? '编辑课程' : '新建课程'" width="480px">
      <el-form label-width="90px">
        <el-form-item label="课程名称">
          <el-input v-model="courseForm.name" placeholder="如 数据结构" />
        </el-form-item>
        <el-form-item label="课程代码">
          <el-input v-model="courseForm.code" placeholder="如 CS201" />
        </el-form-item>
        <el-form-item label="学期">
          <el-input v-model="courseForm.term" placeholder="如 2024-2025-1" />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="courseForm.description" type="textarea" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="courseDialog = false">取消</el-button>
        <el-button type="primary" @click="submitCourse">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="classDialog" title="新建班级" width="480px">
      <el-alert
        type="info"
        :closable="false"
        title="班级按「院系 + 专业 + 班级名称」唯一，已存在时会直接挂到当前课程下"
        style="margin-bottom: 14px"
      />
      <el-form label-width="90px">
        <el-form-item label="院系">
          <el-input v-model="classForm.department" placeholder="如 计算机学院" />
        </el-form-item>
        <el-form-item label="专业">
          <el-input v-model="classForm.major" placeholder="如 软件工程" />
        </el-form-item>
        <el-form-item label="班级名称">
          <el-input v-model="classForm.name" placeholder="如 软工2301" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="classDialog = false">取消</el-button>
        <el-button type="primary" @click="submitClass">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="linkDialog" title="挂载已有班级到当前课程" width="560px">
      <el-select
        v-model="linkSelection"
        multiple
        filterable
        remote
        reserve-keyword
        placeholder="搜索还没挂到本课程的班级"
        :remote-method="searchLinkable"
        :loading="linkSearching"
        style="width: 100%"
      >
        <el-option
          v-for="item in linkOptions"
          :key="item.id"
          :label="item.full_name"
          :value="item.id"
        />
      </el-select>
      <template #footer>
        <el-button @click="linkDialog = false">取消</el-button>
        <el-button type="primary" @click="submitLink">挂载</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="studentDialog" title="添加学生" width="480px">
      <el-form label-width="90px">
        <el-form-item label="学号/工号">
          <el-input v-model="studentForm.student_no" />
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="studentForm.name" />
        </el-form-item>
        <el-form-item label="入学年份">
          <el-input-number v-model="studentForm.enrollment_year" :min="1900" :max="2200" />
        </el-form-item>
        <el-form-item label="加入时间">
          <el-date-picker v-model="studentForm.joined_at" type="date" style="width: 100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="studentDialog = false">取消</el-button>
        <el-button type="primary" @click="submitStudent">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="importDialog" title="导入班级与学生" width="620px">
      <el-alert
        type="success"
        :closable="false"
        :title="`导入到课程：${currentCourse?.name || ''}`"
        style="margin-bottom: 14px"
      />
      <el-alert
        type="info"
        :closable="false"
        title="支持 .csv / .xlsx。表头需包含「学号/工号」和「姓名」，可选：院系、专业、班级、加入时间、入学年份"
        style="margin-bottom: 14px"
      />
      <el-upload :auto-upload="false" :limit="1" :on-change="onImportFile" :show-file-list="true">
        <el-button>选择文件</el-button>
      </el-upload>

      <el-alert v-if="importResult" type="success" :closable="false" style="margin-top: 16px">
        <div>{{ importResult.message }}</div>
        <div v-if="importResult.skipped?.length" class="muted" style="margin-top: 6px">
          跳过 {{ importResult.skipped.length }} 行，例如：{{ importResult.skipped[0].reason }}
        </div>
      </el-alert>

      <template #footer>
        <el-button @click="importDialog = false">关闭</el-button>
        <el-button type="primary" :loading="importing" @click="submitImport">开始导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>
