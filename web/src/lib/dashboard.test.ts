import { describe, expect, it } from 'vitest'

import { filterJobs, type Job } from './dashboard'

const jobs: Job[] = [
  {
    uid: '0000000000000001', company: 'Atlas', title: 'ML Engineer', url: null,
    location: 'Istanbul', workplace: 'hybrid', postedAt: '2026-09-15', source: 'lever:atlas',
    description: '', rawSeniority: '', roleFamily: 'data_ai', opportunityType: 'job', eligibility: 'confirmed', eligibilityReason: 'Istanbul', studentCompatible: false, score: 21, status: 'new', firstSeen: 'r1', lastSeen: 'r1', runId: 'r1', matchedSkills: ['python'],
  },
  {
    uid: '0000000000000002', company: 'Bora', title: 'Data Scientist', url: null,
    location: 'Remote', workplace: 'remote', postedAt: '2026-09-14', source: 'workable:bora',
    description: '', rawSeniority: '', roleFamily: 'data_ai', opportunityType: 'internship', eligibility: 'confirmed', eligibilityReason: 'Turkey', studentCompatible: true, score: 15, status: 'applied', firstSeen: 'r0', lastSeen: 'r1', runId: 'r1', matchedSkills: ['pytorch'],
  },
  {
    uid: '0000000000000003', company: 'Coda', title: 'AI Researcher', url: null,
    location: 'Istanbul', workplace: 'onsite', postedAt: '2026-09-13', source: 'lever:coda',
    description: '', rawSeniority: '', roleFamily: 'research', opportunityType: 'research', eligibility: 'review', eligibilityReason: 'Location unclear', studentCompatible: true, score: 35, status: 'rejected', firstSeen: 'r0', lastSeen: 'r1', runId: 'r1', matchedSkills: ['python'],
  },
]

const base = { query: '', workplace: 'all', source: 'all', roleFamily: 'all', opportunityType: 'all', status: 'all', sort: 'score' as const }

describe('filterJobs', () => {
  it('shows only active workflow states by default and ranks by match score', () => {
    expect(filterJobs(jobs, { ...base, scope: 'active' }).map((job) => job.uid)).toEqual([
      '0000000000000001', '0000000000000002',
    ])
  })

  it('moves rejected and closed work into the archive view', () => {
    expect(filterJobs(jobs, { ...base, scope: 'archive' }).map((job) => job.uid)).toEqual([
      '0000000000000003',
    ])
  })

  it('combines Turkish-safe text, workplace, source, and status filters', () => {
    expect(filterJobs(jobs, {
      ...base, scope: 'active', query: 'pyt', workplace: 'remote', source: 'workable:bora', status: 'applied',
    }).map((job) => job.title)).toEqual(['Data Scientist'])
  })

  it('offers newest and company sorting', () => {
    expect(filterJobs(jobs, { ...base, scope: 'active', sort: 'newest' }).map((job) => job.company)).toEqual(['Atlas', 'Bora'])
    expect(filterJobs(jobs, { ...base, scope: 'active', sort: 'company' }).map((job) => job.company)).toEqual(['Atlas', 'Bora'])
  })

  it('keeps uncertain active jobs out of the default view and exposes the review queue', () => {
    const uncertain = { ...jobs[0], uid: '0000000000000004', eligibility: 'review' as const }
    expect(filterJobs([...jobs, uncertain], { ...base, scope: 'review' }).map((job) => job.uid)).toEqual(['0000000000000004'])
  })
})
