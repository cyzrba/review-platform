import axios from 'axios'

const http = axios.create({ baseURL: '/api', timeout: 180000 })

http.interceptors.response.use(
  (response) => response,
  (error) => {
    const detail = error?.response?.data?.detail
    if (typeof detail === 'string') {
      error.message = detail
    } else if (Array.isArray(detail)) {
      error.message = detail.map((item) => item.msg).join('；')
    }
    return Promise.reject(error)
  },
)

export const API_BASE = '/api'
export const PAGE_SIZE = 20

// ---------- 系统 ----------
export const getDashboard = () => http.get('/dashboard').then((r) => r.data)
export const getHealth = () => http.get('/health').then((r) => r.data)

// ---------- 课程 ----------
export const listCourses = (params = {}) => http.get('/courses', { params }).then((r) => r.data)
export const getCourse = (id) => http.get(`/courses/${id}`).then((r) => r.data)
export const createCourse = (payload) => http.post('/courses', payload).then((r) => r.data)
export const updateCourse = (id, payload) => http.patch(`/courses/${id}`, payload).then((r) => r.data)
export const deleteCourse = (id) => http.delete(`/courses/${id}`)
export const listCourseClasses = (id, params = {}) =>
  http.get(`/courses/${id}/classes`, { params }).then((r) => r.data)
export const linkClasses = (id, classIds) =>
  http.post(`/courses/${id}/classes`, { class_ids: classIds }).then((r) => r.data)
export const unlinkClass = (id, classId) => http.delete(`/courses/${id}/classes/${classId}`)
export const listCourseRubrics = (id, params = {}) =>
  http.get(`/courses/${id}/rubrics`, { params }).then((r) => r.data)
export const linkRubrics = (id, rubricIds) =>
  http.post(`/courses/${id}/rubrics`, { rubric_ids: rubricIds }).then((r) => r.data)
export const unlinkRubric = (id, rubricId) => http.delete(`/courses/${id}/rubrics/${rubricId}`)

// ---------- 班级与学生 ----------
export const listClasses = (params = {}) => http.get('/classes', { params }).then((r) => r.data)
export const getClass = (id) => http.get(`/classes/${id}`).then((r) => r.data)
export const createClass = (payload) => http.post('/classes', payload).then((r) => r.data)
export const updateClass = (id, payload) => http.patch(`/classes/${id}`, payload).then((r) => r.data)
export const deleteClass = (id) => http.delete(`/classes/${id}`)
export const listStudents = (params = {}) => http.get('/students', { params }).then((r) => r.data)
export const createStudent = (payload) => http.post('/students', payload).then((r) => r.data)
export const updateStudent = (id, payload) => http.patch(`/students/${id}`, payload).then((r) => r.data)
export const deleteStudent = (id) => http.delete(`/students/${id}`)
export const importRoster = (file, courseId) => {
  const form = new FormData()
  form.append('file', file)
  form.append('course_id', courseId)
  return http.post('/classes/import', form).then((r) => r.data)
}
export const rosterTemplateUrl = `${API_BASE}/classes/import/template`

// ---------- 评分细则 ----------
export const listRubrics = (params = {}) => http.get('/rubrics', { params }).then((r) => r.data)
export const getRubric = (id) => http.get(`/rubrics/${id}`).then((r) => r.data)
export const createRubric = (payload) => http.post('/rubrics', payload).then((r) => r.data)
export const updateRubric = (id, payload) => http.patch(`/rubrics/${id}`, payload).then((r) => r.data)
export const deleteRubric = (id) => http.delete(`/rubrics/${id}`)

// ---------- 上传与评审 ----------
export const uploadSubmissions = (rubricId, files, { classId = null, courseId = null } = {}) => {
  const form = new FormData()
  Array.from(files).forEach((file) => form.append('files', file))
  if (classId) form.append('class_id', classId)
  if (courseId) form.append('course_id', courseId)
  return http.post(`/rubrics/${rubricId}/submissions`, form).then((r) => r.data)
}
export const runGrading = (rubricId, payload = {}, sync = false) =>
  http.post(`/rubrics/${rubricId}/grade`, payload, { params: { sync } }).then((r) => r.data)
export const runSingleGrading = (resultId, sync = false) =>
  http.post(`/grading-results/${resultId}/grade`, null, { params: { sync } }).then((r) => r.data)
export const listResults = (rubricId, params = {}) =>
  http.get(`/rubrics/${rubricId}/results`, { params }).then((r) => r.data)
export const classSummary = (rubricId, params = {}) =>
  http.get(`/rubrics/${rubricId}/class-summary`, { params }).then((r) => r.data)
export const updateResult = (resultId, payload) =>
  http.patch(`/grading-results/${resultId}`, payload).then((r) => r.data)
export const deleteResult = (resultId) => http.delete(`/grading-results/${resultId}`)
export const uploadHint = (rubricId) => http.get(`/rubrics/${rubricId}/upload-hint`).then((r) => r.data)

// ---------- 导出与文件 ----------
const scopeQuery = (params = {}) => {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') search.append(key, value)
  })
  const text = search.toString()
  return text ? `?${text}` : ''
}
export const exportXlsxUrl = (rubricId, params) =>
  `${API_BASE}/rubrics/${rubricId}/export.xlsx${scopeQuery(params)}`
export const exportCsvUrl = (rubricId, params) =>
  `${API_BASE}/rubrics/${rubricId}/export.csv${scopeQuery(params)}`
export const fileDownloadUrl = (key) => `${API_BASE}/files/download?key=${encodeURIComponent(key)}`

export default http
