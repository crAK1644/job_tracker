export type JobStatus = 'new' | 'interested' | 'applied' | 'rejected' | 'ignored' | 'closed'
export type Scope = 'active' | 'archive'
export type SortOrder = 'score' | 'newest' | 'company'

export interface Job {
  uid: string
  company: string
  title: string
  url: string | null
  location: string
  workplace: string
  postedAt: string
  source: string
  description: string
  rawSeniority: string
  score: number
  status: JobStatus
  firstSeen: string
  lastSeen: string
  runId: string
  matchedSkills: string[]
}

export interface Link { label: string; url: string }

export interface Dashboard {
  runId: string | null
  jobs: Job[]
  counts: Record<JobStatus, number>
  activeStatuses: JobStatus[]
  archiveStatuses: JobStatus[]
  errors: string[]
  sources: Record<string, number>
  cv: { active: boolean; skillCount: number }
  links: { linkedin: Link[]; kariyer: Link[] }
}

interface Filters {
  scope: Scope
  query: string
  workplace: string
  source: string
  status: string
  sort: SortOrder
}

export const statusLabels: Record<JobStatus, string> = {
  new: 'Yeni',
  interested: 'İlgileniyorum',
  applied: 'Başvurdum',
  rejected: 'Reddedildi',
  ignored: 'Yok sayıldı',
  closed: 'Kapandı',
}

export const workplaceLabels: Record<string, string> = {
  remote: 'Uzaktan',
  hybrid: 'Hibrit',
  onsite: 'Ofis',
  unknown: 'Belirsiz',
}

const active = new Set<JobStatus>(['new', 'interested', 'applied'])

export function filterJobs(jobs: Job[], filters: Filters): Job[] {
  const query = filters.query.trim().toLocaleLowerCase('tr-TR')
  return jobs
    .filter((job) => filters.scope === 'active' ? active.has(job.status) : !active.has(job.status))
    .filter((job) => !query || [job.company, job.title, job.location, job.source, ...job.matchedSkills].join(' ').toLocaleLowerCase('tr-TR').includes(query))
    .filter((job) => filters.workplace === 'all' || job.workplace === filters.workplace)
    .filter((job) => filters.source === 'all' || job.source === filters.source)
    .filter((job) => filters.status === 'all' || job.status === filters.status)
    .sort((left, right) => {
      if (filters.sort === 'company') return left.company.localeCompare(right.company, 'tr')
      if (filters.sort === 'newest') return right.postedAt.localeCompare(left.postedAt)
      return right.score - left.score || right.postedAt.localeCompare(left.postedAt)
    })
}

export function formatRunTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value || 'Bilinmiyor'
  return new Intl.DateTimeFormat('tr-TR', { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

export function formatPostedAt(value: string): string {
  if (!value) return 'Tarih belirtilmemiş'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('tr-TR', { day: 'numeric', month: 'short', year: 'numeric' }).format(date)
}
