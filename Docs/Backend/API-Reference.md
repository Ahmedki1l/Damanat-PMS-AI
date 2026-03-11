# Damanat PMS Core Backend — API Reference

> **Base URL:** `http://localhost:3000/api/v1`  
> **Auth:** All protected endpoints require `Authorization: Bearer <accessToken>`  
> **Content-Type:** `application/json`

---

## 0. Health Check

### `GET /health`

No auth required. Returns service status.

**Response `200`:**
```json
{
  "status": "ok",
  "service": "damanat-pms-core-backend",
  "timestamp": "2026-03-08T04:50:00.000Z"
}
```

---

## 1. Authentication

### `POST /auth/login`

Authenticate a user and receive JWT tokens.

**Auth:** None

**Request Body:**
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| `email` | string | ✅ | Valid email |
| `password` | string | ✅ | Non-empty |

**Example:**
```json
{
  "email": "admin@damanat.com",
  "password": "admin123"
}
```

**Response `200`:**
```json
{
  "status": "success",
  "data": {
    "accessToken": "eyJhbG...",
    "refreshToken": "eyJhbG...",
    "user": {
      "id": "uuid",
      "email": "admin@damanat.com",
      "name": "System Admin",
      "role": "SUPER_ADMIN"
    }
  }
}
```

**Errors:** `401` Invalid email or password

---

### `POST /auth/register`

Create a new user account.

**Auth:** Bearer Token — requires `ADMIN` or higher

**Request Body:**
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| `email` | string | ✅ | Valid email |
| `password` | string | ✅ | Min 6 characters |
| `name` | string | ✅ | Non-empty |
| `role` | enum | ❌ | `SUPER_ADMIN` \| `ADMIN` \| `OPERATOR` \| `VIEWER` (default: `VIEWER`) |

**Example:**
```json
{
  "email": "operator@damanat.com",
  "password": "secure123",
  "name": "Ahmed Al-Sayed",
  "role": "OPERATOR"
}
```

**Response `201`:**
```json
{
  "status": "success",
  "data": {
    "id": "uuid",
    "email": "operator@damanat.com",
    "name": "Ahmed Al-Sayed",
    "role": "OPERATOR",
    "createdAt": "2026-03-08T04:50:00.000Z"
  }
}
```

**Errors:** `401` Unauthorized · `403` Requires ADMIN role · `409` Email already registered

---

### `POST /auth/refresh`

Exchange a refresh token for a new access token.

**Auth:** None

**Request Body:**
| Field | Type | Required |
|-------|------|----------|
| `refreshToken` | string | ✅ |

**Response `200`:**
```json
{
  "status": "success",
  "data": {
    "accessToken": "eyJhbG..."
  }
}
```

**Errors:** `401` Invalid or expired refresh token

---

### `GET /auth/me`

Get the authenticated user's profile and site roles.

**Auth:** Bearer Token

**Response `200`:**
```json
{
  "status": "success",
  "data": {
    "id": "uuid",
    "email": "admin@damanat.com",
    "name": "System Admin",
    "role": "SUPER_ADMIN",
    "createdAt": "2026-03-08T04:50:00.000Z",
    "siteRoles": [
      {
        "id": "uuid",
        "siteId": "uuid",
        "role": "ADMIN",
        "site": { "id": "uuid", "name": "Damanat HQ" }
      }
    ]
  }
}
```

---

## 2. Users

> All endpoints require `ADMIN` role or higher.

### `GET /users`

List all users with their site roles.

**Response `200`:**
```json
{
  "status": "success",
  "data": [
    {
      "id": "uuid",
      "email": "user@damanat.com",
      "name": "User Name",
      "role": "OPERATOR",
      "createdAt": "...",
      "updatedAt": "...",
      "siteRoles": [...]
    }
  ]
}
```

---

### `GET /users/:id`

Get a single user by ID.

**Response `200`:** Same structure as list item.  
**Errors:** `404` User not found

---

### `PATCH /users/:id`

Update a user's name or global role.

**Request Body:**
| Field | Type | Required |
|-------|------|----------|
| `name` | string | ❌ |
| `role` | enum | ❌ | `SUPER_ADMIN` \| `ADMIN` \| `OPERATOR` \| `VIEWER` |

**Response `200`:** Updated user object.  
**Errors:** `404` User not found

---

### `DELETE /users/:id`

Delete a user (cascades site roles).

**Response `204`:** No content.  
**Errors:** `404` User not found

---

### `POST /users/:id/site-roles`

Assign or update a user's role for a specific site. Uses upsert — if the user already has a role for the site, it gets updated.

**Request Body:**
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| `siteId` | UUID | ✅ | Valid UUID |
| `role` | enum | ✅ | `ADMIN` \| `OPERATOR` \| `VIEWER` |

**Example:**
```json
{
  "siteId": "550e8400-e29b-41d4-a716-446655440000",
  "role": "OPERATOR"
}
```

**Response `200`:**
```json
{
  "status": "success",
  "data": {
    "id": "uuid",
    "userId": "uuid",
    "siteId": "uuid",
    "role": "OPERATOR",
    "createdAt": "..."
  }
}
```

**Errors:** `404` User or Site not found

---

### `DELETE /users/:id/site-roles/:siteId`

Remove a user's role for a specific site.

**Response `204`:** No content.  
**Errors:** `404` Site role not found

---

## 3. Sites

### `GET /sites`

List sites accessible to the authenticated user. `SUPER_ADMIN` sees all sites; other roles see only their assigned sites.

**Auth:** Bearer Token

**Response `200`:**
```json
{
  "status": "success",
  "data": [
    {
      "id": "uuid",
      "name": "Damanat HQ",
      "code": "DAM-01",
      "location": "Riyadh, Saudi Arabia",
      "description": "Main parking facility",
      "pmsAiBaseUrl": "http://5.5.5.3:8080",
      "pmsAiApiKey": null,
      "createdAt": "...",
      "updatedAt": "...",
      "floors": [
        { "id": "uuid", "name": "Ground Floor", "level": 0 }
      ],
      "_count": { "cameras": 3, "floors": 2 }
    }
  ]
}
```

---

### `POST /sites`

Create a new site.

**Auth:** Bearer Token — requires `SUPER_ADMIN`

**Request Body:**
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| `name` | string | ✅ | Non-empty |
| `code` | string | ❌ | Short identifier |
| `location` | string | ❌ | |
| `description` | string | ❌ | |
| `pmsAiBaseUrl` | string | ❌ | Valid URL |
| `pmsAiApiKey` | string | ❌ | API key for PMS AI |

**Example:**
```json
{
  "name": "Damanat HQ",
  "code": "DAM-01",
  "location": "Riyadh, Saudi Arabia",
  "pmsAiBaseUrl": "http://5.5.5.3:8080"
}
```

**Response `201`:** Created site object.  
**Errors:** `403` Requires SUPER_ADMIN

---

### `GET /sites/:id`

Get detailed site info including floors and zones.

**Auth:** Bearer Token

**Response `200`:** Full site object with nested floors → zones.

---

### `PATCH /sites/:id`

Update site details.

**Auth:** Bearer Token — requires `ADMIN` or higher

**Request Body:** Same fields as create, all optional.

**Response `200`:** Updated site object.  
**Errors:** `404` Site not found

---

### `DELETE /sites/:id`

Delete a site (cascades floors, zones, cameras, site roles).

**Auth:** Bearer Token — requires `SUPER_ADMIN`

**Response `204`:** No content.  
**Errors:** `404` Site not found

---

## 4. Floors

### `GET /sites/:siteId/floors`

List all floors for a site, ordered by level ascending.

**Auth:** Bearer Token

**Response `200`:**
```json
{
  "status": "success",
  "data": [
    {
      "id": "uuid",
      "siteId": "uuid",
      "name": "B1",
      "level": -1,
      "createdAt": "...",
      "updatedAt": "...",
      "zones": [
        { "id": "uuid", "name": "parking-row-A", "type": "PARKING", "maxCapacity": 10 }
      ],
      "_count": { "cameras": 1, "zones": 3 }
    }
  ]
}
```

**Errors:** `404` Site not found

---

### `POST /sites/:siteId/floors`

Create a floor under a site.

**Auth:** Bearer Token — requires `ADMIN` or higher

**Request Body:**
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| `name` | string | ✅ | Non-empty |
| `level` | integer | ❌ | Floor level number (default: 0) |

**Example:**
```json
{
  "name": "Basement 1",
  "level": -1
}
```

**Response `201`:** Created floor object.  
**Errors:** `404` Site not found

---

### `GET /floors/:id`

Get a single floor by ID.

**Auth:** Bearer Token

**Response `200`:** Floor object with zones.

---

### `PATCH /floors/:id`

Update a floor.

**Auth:** Bearer Token — requires `ADMIN` or higher

**Request Body:** `name` and/or `level`, both optional.

**Response `200`:** Updated floor object.  
**Errors:** `404` Floor not found

---

### `DELETE /floors/:id`

Delete a floor (cascades zones and cameras).

**Auth:** Bearer Token — requires `ADMIN` or higher

**Response `204`:** No content.  
**Errors:** `404` Floor not found

---

## 5. Zones

### `GET /floors/:floorId/zones`

List all zones for a floor.

**Auth:** Bearer Token

**Response `200`:**
```json
{
  "status": "success",
  "data": [
    {
      "id": "uuid",
      "floorId": "uuid",
      "name": "parking-row-A",
      "type": "PARKING",
      "description": "Row A — visitor parking",
      "maxCapacity": 25,
      "createdAt": "...",
      "updatedAt": "...",
      "_count": { "cameras": 1 }
    }
  ]
}
```

**Errors:** `404` Floor not found

---

### `POST /floors/:floorId/zones`

Create a zone under a floor.

**Auth:** Bearer Token — requires `ADMIN` or higher

**Request Body:**
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| `name` | string | ✅ | Non-empty |
| `type` | enum | ❌ | `PARKING` \| `RESTRICTED` \| `INTRUSION` \| `OTHER` (default: `PARKING`) |
| `description` | string | ❌ | |
| `maxCapacity` | integer | ❌ | Positive integer (default: 10) |

**Example:**
```json
{
  "name": "parking-row-A",
  "type": "PARKING",
  "description": "Row A — visitor parking",
  "maxCapacity": 25
}
```

**Response `201`:** Created zone object.  
**Errors:** `404` Floor not found

---

### `GET /zones/:id`

Get a single zone by ID.

**Auth:** Bearer Token

**Response `200`:** Zone object.

---

### `PATCH /zones/:id`

Update a zone.

**Auth:** Bearer Token — requires `ADMIN` or higher

**Request Body:** Same fields as create, all optional.

**Response `200`:** Updated zone object.  
**Errors:** `404` Zone not found

---

### `DELETE /zones/:id`

Delete a zone.

**Auth:** Bearer Token — requires `ADMIN` or higher

**Response `204`:** No content.  
**Errors:** `404` Zone not found

---

## 6. Cameras

### `GET /sites/:siteId/cameras`

List all cameras for a site. **Camera passwords are never returned.**

**Auth:** Bearer Token

**Response `200`:**
```json
{
  "status": "success",
  "data": [
    {
      "id": "uuid",
      "siteId": "uuid",
      "floorId": "uuid",
      "zoneId": "uuid",
      "name": "CAM-04",
      "ip": "10.1.13.63",
      "username": "kloudspot",
      "model": "DS-2CD3783G2-IZSU",
      "role": "OCCUPANCY",
      "isActive": true,
      "createdAt": "...",
      "updatedAt": "...",
      "floor": { "id": "uuid", "name": "B1" },
      "zone": { "id": "uuid", "name": "parking-row-A", "type": "PARKING" }
    }
  ]
}
```

**Errors:** `404` Site not found

---

### `POST /sites/:siteId/cameras`

Register a new camera. Password is encrypted (AES-256-GCM) before storage.

**Auth:** Bearer Token — requires `ADMIN` or higher

**Request Body:**
| Field | Type | Required | Validation |
|-------|------|----------|------------|
| `floorId` | UUID | ✅ | Must belong to the site |
| `zoneId` | UUID | ❌ | Must belong to the floor |
| `name` | string | ✅ | e.g. `CAM-04` |
| `ip` | string | ✅ | Camera IP address |
| `username` | string | ✅ | Camera login |
| `password` | string | ✅ | Camera password (plaintext, encrypted at rest) |
| `model` | string | ❌ | Hikvision model number |
| `role` | enum | ❌ | `OCCUPANCY` \| `ENTRY` \| `EXIT` \| `VIOLATION` \| `INTRUSION` (default: `OCCUPANCY`) |

**Example:**
```json
{
  "floorId": "uuid-of-b1",
  "zoneId": "uuid-of-parking-row-a",
  "name": "CAM-04",
  "ip": "10.1.13.63",
  "username": "kloudspot",
  "password": "Kloud@123",
  "model": "DS-2CD3783G2-IZSU",
  "role": "OCCUPANCY"
}
```

**Response `201`:** Created camera object (password excluded).  
**Errors:** `400` Floor does not belong to site · `400` Zone does not belong to floor · `404` Site not found

---

### `GET /cameras/:id`

Get a single camera by ID.

**Auth:** Bearer Token

**Response `200`:** Camera object (password excluded).

---

### `PATCH /cameras/:id`

Update a camera. If `password` is included, it is re-encrypted.

**Auth:** Bearer Token — requires `ADMIN` or higher

**Request Body:**
| Field | Type | Required |
|-------|------|----------|
| `floorId` | UUID | ❌ |
| `zoneId` | UUID | ❌ |
| `name` | string | ❌ |
| `ip` | string | ❌ |
| `username` | string | ❌ |
| `password` | string | ❌ |
| `model` | string | ❌ |
| `role` | enum | ❌ |
| `isActive` | boolean | ❌ |

**Response `200`:** Updated camera object.  
**Errors:** `404` Camera not found

---

### `DELETE /cameras/:id`

Delete a camera.

**Auth:** Bearer Token — requires `ADMIN` or higher

**Response `204`:** No content.  
**Errors:** `404` Camera not found

---

## 7. PMS AI Proxy

Analytics endpoints that read from the shared PostgreSQL database (PMS AI tables). Falls back to HTTP proxy via the site's `pmsAiBaseUrl` for health checks.

### `GET /sites/:id/occupancy`

Get real-time zone occupancy data.

**Auth:** Bearer Token

**Response `200`:**
```json
{
  "status": "success",
  "data": [
    {
      "zoneId": "parking-row-A",
      "cameraId": "CAM-04",
      "currentCount": 8,
      "maxCapacity": 10,
      "percentage": 80,
      "isFull": false,
      "lastUpdated": "2026-03-08T04:45:00.000Z"
    }
  ]
}
```

---

### `GET /sites/:id/alerts`

Get alerts from the PMS AI system.

**Auth:** Bearer Token

**Query Parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `alertType` | string | Filter by type: `occupancy_full` \| `violation` \| `intrusion` \| `unknown_vehicle` |
| `isResolved` | string | `true` or `false` |

**Response `200`:**
```json
{
  "status": "success",
  "data": [
    {
      "id": 1,
      "alertType": "violation",
      "cameraId": "CAM-02",
      "zoneId": "restricted-vip",
      "eventType": "fielddetection",
      "description": "Vehicle in restricted zone: restricted-vip",
      "isResolved": 0,
      "triggeredAt": "2026-03-08T04:30:00.000Z",
      "resolvedAt": null
    }
  ]
}
```

---

### `GET /sites/:id/events`

Get recent camera events (raw event log).

**Auth:** Bearer Token

**Query Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | integer | 50 | Max events to return |

**Response `200`:**
```json
{
  "status": "success",
  "data": [
    {
      "id": 42,
      "cameraId": "CAM-04",
      "eventType": "regionEntrance",
      "eventState": "active",
      "eventDescription": "Region entrance",
      "detectionTarget": "vehicle",
      "regionId": "parking-row-A",
      "snapshotPath": "detection_images/snap_regionEntrance_CAM-04_20260308.jpg",
      "createdAt": "2026-03-08T04:40:00.000Z"
    }
  ]
}
```

---

### `GET /sites/:id/health`

Check PMS AI service health via HTTP proxy.

**Auth:** Bearer Token

**Response `200` (reachable):**
```json
{
  "status": "success",
  "data": {
    "status": "ok",
    "cameras": ["CAM-04", "CAM-02", "CAM-35"]
  }
}
```

**Response `200` (unreachable):**
```json
{
  "status": "success",
  "data": {
    "status": "unreachable",
    "error": "fetch failed"
  }
}
```

---

## 8. Parking Times (MongoDB)

Vehicle entry/exit session tracking stored in MongoDB. One document per license plate, structured as **plate → day → sessions**.

> **Data source:** MongoDB `parking_times` collection  
> **Local setup:** `docker-compose up -d` (spins up Mongo 7 on port 27017)

### `POST /sites/:siteId/parking-times/entry`

Record a vehicle entry. Creates the plate document if new; pushes a session with `exitAt: null`.

**Auth:** Bearer Token

**Request Body:**
| Field | Type | Required |
|-------|------|----------|
| `plate` | string | ✅ |
| `entryAt` | ISO string | ❌ (defaults to now) |
| `entryCamera` | string | ✅ |
| `zone` | string | ❌ |

**Example:**
```json
{
  "plate": "ABC-1234",
  "entryCamera": "CAM-01",
  "zone": "parking-row-A"
}
```

**Response `201`:**
```json
{
  "status": "success",
  "data": {
    "sessionId": "uuid",
    "plate": "ABC-1234",
    "day": "2026-03-10",
    "entryAt": "2026-03-10T08:12:00.000Z"
  }
}
```

---

### `POST /sites/:siteId/parking-times/exit`

Record a vehicle exit. Closes the latest open session for today, computes `durationMinutes`.

**Auth:** Bearer Token

**Request Body:**
| Field | Type | Required |
|-------|------|----------|
| `plate` | string | ✅ |
| `exitAt` | ISO string | ❌ (defaults to now) |
| `exitCamera` | string | ✅ |

**Example:**
```json
{
  "plate": "ABC-1234",
  "exitCamera": "CAM-03"
}
```

**Response `200`:**
```json
{
  "status": "success",
  "data": {
    "plate": "ABC-1234",
    "sessionId": "uuid",
    "day": "2026-03-10",
    "exitAt": "2026-03-10T09:45:00.000Z",
    "durationMinutes": 93
  }
}
```

**Errors:** `404` Plate not found · `400` No open session

---

### `GET /sites/:siteId/parking-times/:plate`

Get full history for a license plate.

**Auth:** Bearer Token

**Response `200`:**
```json
{
  "status": "success",
  "data": {
    "plate": "ABC-1234",
    "siteId": "uuid",
    "totalVisits": 15,
    "firstSeen": "2026-03-01T08:00:00Z",
    "lastSeen": "2026-03-10T14:00:00Z",
    "days": {
      "2026-03-10": {
        "sessions": [
          {
            "sessionId": "uuid",
            "entryAt": "2026-03-10T08:12:00Z",
            "exitAt": "2026-03-10T09:45:00Z",
            "durationMinutes": 93,
            "entryCamera": "CAM-01",
            "exitCamera": "CAM-03",
            "zone": "parking-row-A"
          }
        ],
        "totalSessions": 1,
        "totalDurationMinutes": 93
      }
    }
  }
}
```

---

### `GET /sites/:siteId/parking-times/:plate/day/:date`

Get sessions for a specific day (`date` = `YYYY-MM-DD`).

**Auth:** Bearer Token

**Response `200`:**
```json
{
  "status": "success",
  "data": {
    "plate": "ABC-1234",
    "date": "2026-03-10",
    "sessions": [...],
    "totalSessions": 2,
    "totalDurationMinutes": 150
  }
}
```

---

### `GET /sites/:siteId/parking-times/active`

List all vehicles currently parked (open sessions today where `exitAt` is null).

**Auth:** Bearer Token

**Response `200`:**
```json
{
  "status": "success",
  "data": [
    {
      "plate": "ABC-1234",
      "entryAt": "2026-03-10T14:00:00Z",
      "entryCamera": "CAM-01",
      "zone": "parking-row-A",
      "firstSeen": "2026-03-01T08:00:00Z"
    }
  ]
}
```

---

### `GET /sites/:siteId/parking-times/stats`

Aggregate parking statistics for the site.

**Auth:** Bearer Token

**Response `200`:**
```json
{
  "status": "success",
  "data": {
    "totalPlates": 142,
    "activeNow": 8,
    "totalVisits": 1560,
    "avgVisitsPerPlate": 11.0,
    "repeatVisitors": 98,
    "date": "2026-03-10"
  }
}
```

---

## Error Format

All errors follow a consistent format:

```json
{
  "status": "error",
  "message": "Descriptive error message"
}
```

| Code | Meaning |
|------|---------|
| `400` | Validation error or bad request |
| `401` | Missing/invalid token or credentials |
| `403` | Insufficient role permissions |
| `404` | Resource not found |
| `409` | Conflict (e.g. duplicate email) |
| `500` | Internal server error |

---

## Role Hierarchy

```
SUPER_ADMIN > ADMIN > OPERATOR > VIEWER
```

| Role | Capabilities |
|------|-------------|
| `SUPER_ADMIN` | Full access. Create/delete sites. Manage all users. |
| `ADMIN` | Manage floors, zones, cameras, users. Update sites. |
| `OPERATOR` | View all resources. (Write access expandable per-site.) |
| `VIEWER` | Read-only access to assigned sites. |

---

## Authentication Flow

```
1. POST /auth/login        → { accessToken, refreshToken }
2. Use accessToken in:       Authorization: Bearer <accessToken>
3. When token expires:       POST /auth/refresh { refreshToken }
4. Access token lifetime:    15 minutes (configurable)
5. Refresh token lifetime:   7 days (configurable)
```
