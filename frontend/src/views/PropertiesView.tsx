import React, { useState } from "react";
import { Plus, MapPin, Layers, X } from "lucide-react";
import { api } from "../api";
import type { Property } from "../types";

interface PropertiesViewProps {
  properties: Property[];
  onRefresh: () => void;
}

export const PropertiesView: React.FC<PropertiesViewProps> = ({ properties, onRefresh }) => {
  const [openAddModal, setOpenAddModal] = useState(false);
  const [filterType, setFilterType] = useState("ALL");

  // Form states
  const [title, setTitle] = useState("");
  const [propertyType, setPropertyType] = useState("APARTMENT");
  const [bhk, setBhk] = useState("3");
  const [price, setPrice] = useState("");
  const [locationText, setLocationText] = useState("");
  const [areaSqft, setAreaSqft] = useState("");
  const [description, setDescription] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleAddSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      await api.createProperty({
        title,
        property_type: propertyType,
        bhk: bhk ? parseInt(bhk, 10) : undefined,
        price: parseInt(price, 10),
        location_text: locationText,
        area_sqft: areaSqft ? parseInt(areaSqft, 10) : undefined,
        description: description || undefined,
        amenities: ["PARKING", "SECURITY", "ELEVATOR"],
      });
      setOpenAddModal(false);
      onRefresh();
      setTitle("");
      setPrice("");
      setLocationText("");
    } catch (err: any) {
      setError(err.message || "Failed to create property.");
    } finally {
      setLoading(false);
    }
  };

  const filtered = properties.filter((p) => {
    if (filterType !== "ALL" && p.property_type !== filterType) return false;
    return true;
  });

  return (
    <div>
      <div className="page-header">
        <div>
          <h1 className="page-title">Property Inventory Catalog</h1>
          <p className="page-subtitle">Verified real estate inventory available for automated matching and client showings</p>
        </div>
        <button className="btn btn-primary" onClick={() => setOpenAddModal(true)}>
          <Plus size={16} /> Add New Property
        </button>
      </div>

      {/* Filter Tabs */}
      <div style={{ display: "flex", gap: "8px", marginBottom: "24px" }}>
        {["ALL", "APARTMENT", "VILLA", "PLOT", "COMMERCIAL"].map((t) => (
          <button
            key={t}
            onClick={() => setFilterType(t)}
            className={filterType === t ? "btn btn-primary" : "btn btn-secondary"}
            style={{ fontSize: "0.8125rem", padding: "6px 14px" }}
          >
            {t}
          </button>
        ))}
      </div>

      {/* Properties Grid */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))",
          gap: "24px",
        }}
      >
        {filtered.length === 0 ? (
          <div style={{ gridColumn: "1 / -1", textAlign: "center", padding: "60px", color: "var(--text-muted)" }}>
            No properties found in this category. Click "Add New Property" to add listings to the catalog.
          </div>
        ) : (
          filtered.map((prop) => (
            <div key={prop.id} className="glass-card" style={{ overflow: "hidden", display: "flex", flexDirection: "column" }}>
              {/* Photo Banner */}
              <div
                style={{
                  height: "160px",
                  backgroundColor: "#1e293b",
                  backgroundImage: prop.photos && prop.photos.length > 0 ? `url(${prop.photos[0]})` : "none",
                  backgroundSize: "cover",
                  backgroundPosition: "center",
                  position: "relative",
                  display: "flex",
                  alignItems: "flex-end",
                  padding: "12px",
                }}
              >
                <span className="badge badge-success" style={{ position: "absolute", top: "12px", right: "12px" }}>
                  {prop.status}
                </span>
                <span
                  style={{
                    backgroundColor: "rgba(0, 0, 0, 0.75)",
                    backdropFilter: "blur(4px)",
                    color: "#fff",
                    padding: "4px 8px",
                    borderRadius: "4px",
                    fontSize: "0.75rem",
                    fontWeight: 600,
                  }}
                >
                  {prop.property_type}
                </span>
              </div>

              {/* Content Body */}
              <div style={{ padding: "18px", flex: 1, display: "flex", flexDirection: "column", justifyContent: "space-between" }}>
                <div>
                  <h3 style={{ fontSize: "1.1rem", marginBottom: "6px" }}>{prop.title}</h3>
                  <div style={{ display: "flex", alignItems: "center", gap: "4px", fontSize: "0.8125rem", color: "var(--text-muted)", marginBottom: "14px" }}>
                    <MapPin size={14} />
                    <span>{prop.location_text}</span>
                  </div>

                  <div style={{ display: "flex", gap: "12px", fontSize: "0.8125rem", color: "var(--text-secondary)", marginBottom: "16px" }}>
                    {prop.bhk && (
                      <span style={{ display: "flex", alignItems: "center", gap: "4px" }}>
                        <Layers size={14} /> {prop.bhk} BHK
                      </span>
                    )}
                    {prop.area_sqft && <span>{prop.area_sqft} sq.ft</span>}
                  </div>
                </div>

                {/* Price Footer */}
                <div style={{ paddingTop: "14px", borderTop: "1px solid var(--border-subtle)", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <div style={{ fontSize: "1.25rem", fontWeight: 700, color: "var(--text-primary)" }}>
                    ₹{(prop.price / 10000000).toFixed(2)} Cr
                  </div>
                  <button className="btn btn-secondary" style={{ fontSize: "0.75rem", padding: "4px 10px" }}>
                    Details
                  </button>
                </div>
              </div>
            </div>
          ))
        )}
      </div>

      {/* Add Property Modal */}
      {openAddModal && (
        <div className="modal-backdrop">
          <div className="modal-content" style={{ maxWidth: "500px" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
              <h3 style={{ fontSize: "1.25rem" }}>Add Property to Inventory</h3>
              <button onClick={() => setOpenAddModal(false)} style={{ background: "none", border: "none", color: "var(--text-muted)", cursor: "pointer" }}>
                <X size={18} />
              </button>
            </div>

            {error && (
              <div style={{ padding: "8px 12px", backgroundColor: "rgba(239, 68, 68, 0.15)", color: "#f87171", borderRadius: "6px", fontSize: "0.8125rem", marginBottom: "14px" }}>
                {error}
              </div>
            )}

            <form onSubmit={handleAddSubmit} style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Property Title *</label>
                <input required type="text" className="form-control" placeholder="e.g. Prestige High Fields 3BHK" value={title} onChange={(e) => setTitle(e.target.value)} />
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
                <div>
                  <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Type</label>
                  <select className="form-control" value={propertyType} onChange={(e) => setPropertyType(e.target.value)}>
                    <option value="APARTMENT">Apartment</option>
                    <option value="VILLA">Villa</option>
                    <option value="PLOT">Plot</option>
                    <option value="COMMERCIAL">Commercial</option>
                  </select>
                </div>

                <div>
                  <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>BHK</label>
                  <select className="form-control" value={bhk} onChange={(e) => setBhk(e.target.value)}>
                    <option value="1">1 BHK</option>
                    <option value="2">2 BHK</option>
                    <option value="3">3 BHK</option>
                    <option value="4">4 BHK</option>
                    <option value="5">5+ BHK</option>
                  </select>
                </div>
              </div>

              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
                <div>
                  <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Price (INR) *</label>
                  <input required type="number" className="form-control" placeholder="18500000" value={price} onChange={(e) => setPrice(e.target.value)} />
                </div>

                <div>
                  <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Area (SqFt)</label>
                  <input type="number" className="form-control" placeholder="1850" value={areaSqft} onChange={(e) => setAreaSqft(e.target.value)} />
                </div>
              </div>

              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Location Text *</label>
                <input required type="text" className="form-control" placeholder="Koramangala 4th Block, Bangalore" value={locationText} onChange={(e) => setLocationText(e.target.value)} />
              </div>

              <div>
                <label style={{ fontSize: "0.75rem", color: "var(--text-secondary)", display: "block", marginBottom: "4px" }}>Description</label>
                <textarea className="form-control" rows={2} placeholder="Luxury high-rise apartment with panoramic view..." value={description} onChange={(e) => setDescription(e.target.value)} />
              </div>

              <button type="submit" className="btn btn-primary" style={{ height: "42px", marginTop: "8px" }} disabled={loading}>
                {loading ? "Adding..." : "Add to Verified Inventory"}
              </button>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};
