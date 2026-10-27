<script setup>
import { MoreFilled, Plus, Search, UploadFilled } from '@element-plus/icons-vue'
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  createCourse,
  deleteCourse,
  getIstudyStatus,
  importIstudyRoster,
  listCourseClasses,
  listCourses,
  listIstudyCourses,
  listStudents,
  updateCourse,
} from '@/api'

const emptyPage = () => ({ items: [], total: 0, page: 1, page_size: 20, pages: 0 })

const courses = ref(emptyPage())
const classes = ref(emptyPage())
const students = ref(emptyPage())
const loading = reactive({ courses: false, classes: false, students: false })
const courseQuery = ref('')
const courseId = ref(null)
const classId = ref(null)

const courseDialog = ref(false)
const courseForm = reactive({ id: null, name: '', code: '', term: '', description: '' })

const rosterDialog = ref(false)
const istudyStatus = ref(null)
const istudyCourses = ref([])
const istudyCourseKey = ref('')
const replaceBeforeImport = ref(true)
const syncingRoster = ref(false)
const syncSummary = ref(null)

const currentCourse = computed(
  () => courses.value.items.find((item) => item.id === courseId.value) || null,
)
const currentClass = computed(
  () => classes.value.items.find((item) => item.id === classId.value) || null,
)

function formatDate(value) {
  return value ? String(value).replace('T', ' ').slice(0, 10) : '—'
}

async function loadCourses(targetPage = 1) {
  loading.courses = true
  try {
    courses.value = await listCourses({
      page: targetPage,
      page_size: 20,
      q: courseQuery.value || undefined,
    })

    if (!courses.value.items.some((item) => item.id === courseId.value)) {
      courseId.value = courses.value.items[0]?.id ?? null
      classId.value = null
      await loadClasses()
    }
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    loading.courses = false
  }
}

async function loadClasses() {
  if (!courseId.value) {
    classes.value = emptyPage()
    students.value = emptyPage()
    classId.value = null
    return
  }

  loading.classes = true
  try {
    classes.value = await listCourseClasses(courseId.value, { page: 1, page_size: 200 })
    if (!classes.value.items.some((item) => item.id === classId.value)) {
      classId.value = classes.value.items[0]?.id ?? null
    }
    await loadStudents()
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    loading.classes = false
  }
}

async function loadStudents(targetPage = 1) {
  if (!classId.value) {
    students.value = emptyPage()
    return
  }

  loading.students = true
  try {
    students.value = await listStudents({
      class_id: classId.value,
      page: targetPage,
      page_size: 20,
    })
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    loading.students = false
  }
}

async function selectCourse(item) {
  if (courseId.value === item.id) return
  courseId.value = item.id
  classId.value = null
  await loadClasses()
}

async function selectClass(item) {
  if (classId.value === item.id) return
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
    name: courseForm.name.trim(),
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
      return
    }

    const created = await createCourse(payload)
    ElMessage.success('课程已创建，可以导入班级与学生')
    courseDialog.value = false
    await loadCourses(1)
    courseId.value = created.id
    classId.value = null
    await loadClasses()
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function removeCourse(item) {
  try {
    await ElMessageBox.confirm(
      `删除课程「${item.name}」？班级和学生数据会保留。`,
      '删除课程',
      { type: 'warning', confirmButtonText: '删除' },
    )
  } catch {
    return
  }

  try {
    await deleteCourse(item.id)
    ElMessage.success('课程已删除')
    if (courseId.value === item.id) courseId.value = null
    await loadCourses(1)
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function openRosterDialog() {
  if (!courseId.value) {
    ElMessage.warning('请先添加或选择课程')
    return
  }

  rosterDialog.value = true
  syncSummary.value = null
  istudyStatus.value = null
  istudyCourses.value = []
  istudyCourseKey.value = ''

  try {
    istudyStatus.value = await getIstudyStatus()
    if (!istudyStatus.value.available) return

    istudyCourses.value = await listIstudyCourses()
    const matched = istudyCourses.value.find(
      (item) => item.name.trim() === currentCourse.value?.name.trim(),
    )
    const target = matched || istudyCourses.value[0]
    if (target) istudyCourseKey.value = `${target.cid}|${target.cpi}`
  } catch (error) {
    ElMessage.error(error.message)
  }
}

async function submitRosterSync() {
  if (!courseId.value || !istudyCourseKey.value) {
    ElMessage.warning('请选择要导入的 i学习 课程')
    return
  }

  const [cid, cpi] = istudyCourseKey.value.split('|')
  syncingRoster.value = true
  syncSummary.value = null
  try {
    syncSummary.value = await importIstudyRoster({
      course_id: courseId.value,
      cid: cid || null,
      cpi: cpi || null,
      source_course_name: currentCourse.value?.name || null,
      replace: replaceBeforeImport.value,
    })
    ElMessage.success(syncSummary.value.message)
    await loadCourses(courses.value.page)
    await loadClasses()
  } catch (error) {
    ElMessage.error(error.message)
  } finally {
    syncingRoster.value = false
  }
}

onMounted(loadCourses)
</script>

<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">班级与学生</h2>
      </div>
      <div class="page-actions">
        <el-button :icon="Plus" :type="courseId ? 'default' : 'primary'" @click="openCourseDialog()">
          添加课程
        </el-button>
        <el-button
          type="primary"
          :icon="UploadFilled"
          :disabled="!courseId"
          @click="openRosterDialog"
        >
          一键导入数据
        </el-button>
      </div>
    </div>

    <div class="course-roster-grid">
      <aside class="table-card course-panel">
        <div class="panel-head">
          <span>课程</span>
          <span class="panel-count">{{ courses.total }}</span>
        </div>
        <el-input
          v-model="courseQuery"
          placeholder="搜索课程名称或代码"
          clearable
          :prefix-icon="Search"
          @keyup.enter="loadCourses(1)"
          @clear="loadCourses(1)"
        />

        <div class="course-list" v-loading="loading.courses">
          <el-empty
            v-if="!courses.items.length"
            description="还没有课程，请先添加课程"
            :image-size="72"
          />
          <article
            v-for="item in courses.items"
            :key="item.id"
            class="course-card"
            :class="{ active: item.id === courseId }"
            tabindex="0"
            @click="selectCourse(item)"
            @keyup.enter="selectCourse(item)"
          >
            <div class="course-card-head">
              <div>
                <strong>{{ item.name }}</strong>
                <span>{{ item.code || '未填写课程代码' }}{{ item.term ? ` · ${item.term}` : '' }}</span>
              </div>
              <el-dropdown
                trigger="click"
                @command="(command) => command === 'edit' ? openCourseDialog(item) : removeCourse(item)"
              >
                <el-button text circle :icon="MoreFilled" aria-label="课程操作" @click.stop />
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="edit">编辑课程</el-dropdown-item>
                    <el-dropdown-item command="delete" divided>删除课程</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
            <div class="course-card-stats">
              <span><strong>{{ item.class_count }}</strong> 个班级</span>
              <span><strong>{{ item.student_count }}</strong> 名学生</span>
            </div>
          </article>
        </div>

        <div class="pager" v-if="courses.pages > 1">
          <el-pagination
            small
            layout="prev, pager, next"
            :current-page="courses.page"
            :page-size="courses.page_size"
            :total="courses.total"
            @current-change="loadCourses"
          />
        </div>
      </aside>

      <section class="table-card roster-panel">
        <div class="panel-head roster-panel-head">
          <div>
            <span>{{ currentCourse?.name || '班级与学生' }}</span>
            <small v-if="currentCourse">
              {{ currentCourse.class_count }} 个班级 · {{ currentCourse.student_count }} 名学生
            </small>
          </div>
          <el-button
            v-if="currentCourse && !classes.items.length"
            type="primary"
            :icon="UploadFilled"
            @click="openRosterDialog"
          >
            导入数据
          </el-button>
        </div>

        <el-empty
          v-if="!currentCourse"
          description="添加课程后，即可从 i学习 一键导入班级与学生"
          :image-size="88"
        >
          <el-button type="primary" :icon="Plus" @click="openCourseDialog()">
            添加课程
          </el-button>
        </el-empty>

        <template v-else>
          <div class="roster-overview">
            <div class="roster-stat">
              <span>班级</span>
              <strong>{{ currentCourse.class_count }}</strong>
            </div>
            <div class="roster-stat">
              <span>学生</span>
              <strong>{{ currentCourse.student_count }}</strong>
            </div>
            <div class="roster-stat roster-stat-wide">
              <span>数据来源</span>
              <strong>i学习</strong>
              <small>一键导入后自动保存到本地</small>
            </div>
          </div>

          <div v-if="classes.items.length" class="class-section">
            <div class="section-title">
              <span>班级</span>
              <small>选择班级查看学生</small>
            </div>
            <div class="class-choice-list" v-loading="loading.classes">
              <button
                v-for="item in classes.items"
                :key="item.id"
                class="class-choice"
                :class="{ active: item.id === classId }"
                @click="selectClass(item)"
              >
                <strong>{{ item.name }}</strong>
                <span>{{ item.department }} · {{ item.major || '未填专业' }}</span>
                <small>{{ item.student_count }} 人</small>
              </button>
            </div>
          </div>

          <div v-if="currentClass" class="student-section">
            <div class="section-title">
              <span>{{ currentClass.name }} · 学生名单</span>
              <small>共 {{ students.total }} 人</small>
            </div>
            <el-table :data="students.items" v-loading="loading.students" empty-text="暂无学生">
              <el-table-column prop="student_no" label="学号 / 工号" min-width="160" />
              <el-table-column prop="name" label="姓名" min-width="120" />
              <el-table-column prop="enrollment_year" label="入学年份" min-width="120" align="center">
                <template #default="{ row }">{{ row.enrollment_year ?? '—' }}</template>
              </el-table-column>
              <el-table-column label="加入时间" min-width="150" align="center">
                <template #default="{ row }">{{ formatDate(row.joined_at) }}</template>
              </el-table-column>
            </el-table>
            <div class="pager" v-if="students.pages > 1">
              <el-pagination
                layout="total, prev, pager, next"
                :current-page="students.page"
                :page-size="students.page_size"
                :total="students.total"
                @current-change="loadStudents"
              />
            </div>
          </div>

          <el-empty
            v-else-if="!classes.items.length"
            description="还没有导入班级与学生"
            :image-size="80"
          />
        </template>
      </section>
    </div>

    <el-dialog
      v-model="courseDialog"
      :title="courseForm.id ? '编辑课程' : '添加课程'"
      width="460px"
    >
      <el-form label-position="top">
        <el-form-item label="课程名称" required>
          <el-input v-model="courseForm.name" placeholder="例如：数据结构" />
        </el-form-item>
        <div class="form-row">
          <el-form-item label="课程代码">
            <el-input v-model="courseForm.code" placeholder="例如：CS201" />
          </el-form-item>
          <el-form-item label="学期">
            <el-input v-model="courseForm.term" placeholder="例如：2026-1" />
          </el-form-item>
        </div>
        <el-form-item label="备注">
          <el-input v-model="courseForm.description" type="textarea" :rows="3" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="courseDialog = false">取消</el-button>
        <el-button type="primary" @click="submitCourse">
          {{ courseForm.id ? '保存修改' : '添加课程' }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="rosterDialog" title="一键导入班级与学生" width="620px">
      <div class="import-destination">
        <span>导入到课程</span>
        <strong>{{ currentCourse?.name }}</strong>
      </div>

      <el-alert
        v-if="istudyStatus && !istudyStatus.available"
        type="error"
        :closable="false"
        title="无法连接 i学习"
        :description="istudyStatus.message"
      />
      <template v-else>
        <el-form label-position="top">
          <el-form-item label="i学习 课程">
            <el-select v-model="istudyCourseKey" style="width: 100%" placeholder="选择要导入的课程">
              <el-option
                v-for="item in istudyCourses"
                :key="item.cid"
                :label="item.name"
                :value="`${item.cid}|${item.cpi}`"
              />
            </el-select>
          </el-form-item>
        </el-form>

        <el-checkbox v-model="replaceBeforeImport">导入前清空当前课程已有名单</el-checkbox>
        <p class="import-help">
          已存在的班级和学生可按学号自动更新；换学期或需要重新整理名单时再勾选覆盖。
        </p>

        <el-alert
          v-if="syncSummary"
          type="success"
          :closable="false"
          style="margin-top: 16px"
          :title="syncSummary.message"
        />
      </template>

      <template #footer>
        <el-button @click="rosterDialog = false">关闭</el-button>
        <el-button
          type="primary"
          :loading="syncingRoster"
          :disabled="!istudyStatus?.available || !istudyStatus?.logged_in || !istudyCourseKey"
          @click="submitRosterSync"
        >
          开始导入
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>
